"""Live arbitrage chain scanner — the worker that closes B11.

Pipeline:

    ChainSource → ChainSnapshot → scan_all() → Opportunity[]
         ↓                                          ↓
    update _LATEST_CHAIN_SNAPSHOT          push to subscribers (WS)

Subscribers can be: gateway WebSocket clients, CLI consoles, local consumers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from qbacktest.core.calendar import IST
from qbacktest.core.chain import ChainSnapshot
from qbacktest.data.chain_source import ChainSource
from qbacktest.strategies.arbitrage import Opportunity, scan_all

log = logging.getLogger(__name__)


# Type for snapshot subscribers (called once per fresh scan)
SnapshotSubscriber = Callable[[ChainSnapshot, list[Opportunity]], Awaitable[None]]


@dataclass
class ChainScannerWorker:
    """Long-running worker that scans an option chain on a fixed cadence.

    Parameters
    ----------
    source
        Anything implementing the ChainSource protocol (Mock, LiveBroker, ...).
    include_far_otm
        Whether to also surface the "loaded gun" far-OTM signals.
    min_edge_after_costs_inr
        Filter out opportunities below this net edge.
    """

    source: ChainSource
    include_far_otm: bool = False
    min_edge_after_costs_inr: float = 0.0
    _subscribers: list[SnapshotSubscriber] = field(default_factory=list, init=False)
    _running: bool = field(default=False, init=False)
    _last_snapshot: Optional[ChainSnapshot] = field(default=None, init=False)
    _last_opportunities: list[Opportunity] = field(default_factory=list, init=False)
    _last_scan_ts: Optional[datetime] = field(default=None, init=False)
    _scan_count: int = field(default=0, init=False)
    _opp_count_total: int = field(default=0, init=False)

    def subscribe(self, callback: SnapshotSubscriber) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: SnapshotSubscriber) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def run(self, *, max_iterations: Optional[int] = None) -> None:
        """Drive the scanner. Runs until stop() is called or max_iterations hit."""
        await self.source.start()
        self._running = True
        try:
            while self._running:
                if max_iterations is not None and self._scan_count >= max_iterations:
                    break
                try:
                    snap = await self.source.next()
                except Exception as e:
                    log.exception("Chain source error: %s", e)
                    await asyncio.sleep(1.0)
                    continue
                opps = scan_all(snap, include_far_otm=self.include_far_otm)
                opps = [o for o in opps if o.edge_after_costs_inr >= self.min_edge_after_costs_inr]
                self._last_snapshot = snap
                self._last_opportunities = opps
                self._last_scan_ts = datetime.now(IST)
                self._scan_count += 1
                self._opp_count_total += len(opps)
                # Fan out to subscribers
                for sub in list(self._subscribers):
                    try:
                        await sub(snap, opps)
                    except Exception:
                        log.exception("Subscriber error")
        finally:
            await self.source.stop()
            self._running = False

    def stop(self) -> None:
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "scans_completed": self._scan_count,
            "opportunities_emitted": self._opp_count_total,
            "last_scan_ts": self._last_scan_ts.isoformat() if self._last_scan_ts else None,
            "last_opportunities": len(self._last_opportunities),
            "running": self._running,
        }

    @property
    def latest(self) -> tuple[Optional[ChainSnapshot], list[Opportunity]]:
        return self._last_snapshot, list(self._last_opportunities)
