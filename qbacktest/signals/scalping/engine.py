"""ScalpingEngine — drives detectors against an intraday bar/tick stream.

Same pipeline shape as ChainScannerWorker:
    BarSource → detectors → TradeSignal[] → subscribers (UI, WS, CLI)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Iterable, Optional, Protocol

from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar
from qbacktest.signals.regime_filter import (
    DEFAULT_REGIME_FILTER,
    RegimeFilter,
    StrategyKind,
)
from qbacktest.signals.scalping.gap_fade import GapFadeDetector
from qbacktest.signals.scalping.orb import ORBDetector
from qbacktest.signals.scalping.reversion import LastHourReversionDetector
from qbacktest.signals.scalping.types import (
    SignalKind,
    SignalStatus,
    TradeSignal,
)

log = logging.getLogger(__name__)


class BarSource(Protocol):
    """Async source of intraday bars."""

    async def next(self) -> Bar: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


SignalSubscriber = Callable[[TradeSignal], Awaitable[None]]


# Map scalping signal kinds to the StrategyKind the regime filter understands
_REGIME_KIND_MAP = {
    SignalKind.ORB_BREAKOUT: StrategyKind.DIRECTIONAL,
    SignalKind.GAP_FADE: StrategyKind.SCALPING,
    SignalKind.LAST_HOUR_REVERSION: StrategyKind.MEAN_REVERSION,
    SignalKind.EOD_SQUARE_OFF: StrategyKind.SCALPING,
}


@dataclass
class ScalpingEngine:
    """Long-running worker. Pulls bars from `source`, runs all detectors, fans
    signals to subscribers, optionally applies regime filter.
    """

    source: BarSource
    detectors: Iterable = field(default_factory=lambda: (
        ORBDetector(),
        GapFadeDetector(),
        LastHourReversionDetector(),
    ))
    regime_filter: Optional[RegimeFilter] = field(default_factory=lambda: DEFAULT_REGIME_FILTER)
    apply_regime: bool = True
    min_expectancy_inr: float = 0.0     # filter out signals with negative or near-zero E
    _subscribers: list[SignalSubscriber] = field(default_factory=list, init=False)
    _running: bool = field(default=False, init=False)
    _signals_today: list[TradeSignal] = field(default_factory=list, init=False)
    _bar_count: int = field(default=0, init=False)
    _signal_count: int = field(default=0, init=False)
    _skipped_count: int = field(default=0, init=False)

    def subscribe(self, cb: SignalSubscriber) -> None:
        self._subscribers.append(cb)

    def unsubscribe(self, cb: SignalSubscriber) -> None:
        try:
            self._subscribers.remove(cb)
        except ValueError:
            pass

    async def run(self, *, max_bars: Optional[int] = None) -> None:
        await self.source.start()
        self._running = True
        try:
            while self._running:
                if max_bars is not None and self._bar_count >= max_bars:
                    break
                try:
                    bar = await self.source.next()
                except Exception as e:
                    log.exception("BarSource error: %s", e)
                    await asyncio.sleep(0.5)
                    continue
                self._bar_count += 1
                await self._process_bar(bar)
        finally:
            await self.source.stop()
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def _process_bar(self, bar: Bar) -> None:
        for det in self.detectors:
            try:
                sig: Optional[TradeSignal] = det.on_bar(bar)
            except Exception:
                log.exception("Detector %s failed on bar %s", det.__class__.__name__, bar.ts)
                sig = None
            if sig is None:
                continue
            # Expectancy threshold
            if sig.expectancy_inr_per_lot < self.min_expectancy_inr:
                self._skipped_count += 1
                continue
            # Regime filter
            sig = self._apply_regime(sig)
            if sig.status == SignalStatus.SKIPPED:
                self._skipped_count += 1
                continue
            self._signals_today.append(sig)
            self._signal_count += 1
            for cb in list(self._subscribers):
                try:
                    await cb(sig)
                except Exception:
                    log.exception("Subscriber error")

    def _apply_regime(self, sig: TradeSignal) -> TradeSignal:
        if not self.apply_regime or self.regime_filter is None:
            return sig
        strat_kind = _REGIME_KIND_MAP.get(sig.kind, StrategyKind.SCALPING)
        d = self.regime_filter.check(sig.ts.date(), kind=strat_kind, ticker=sig.ticker)
        if not d.allowed:
            from dataclasses import replace
            return replace(sig, status=SignalStatus.SKIPPED,
                           regime_note=d.reason, size_multiplier=0.0)
        # Apply size multiplier if regime suggests reduction
        if d.suggested_size_multiplier != 1.0:
            from dataclasses import replace
            new_qty = max(1, int(sig.quantity * d.suggested_size_multiplier))
            return replace(sig, quantity=new_qty,
                           size_multiplier=d.suggested_size_multiplier,
                           regime_note=d.reason)
        return sig

    @property
    def stats(self) -> dict:
        return {
            "bars_processed": self._bar_count,
            "signals_emitted": self._signal_count,
            "signals_skipped": self._skipped_count,
            "signals_today": len(self._signals_today),
            "running": self._running,
        }

    @property
    def latest_signals(self) -> list[TradeSignal]:
        return list(self._signals_today)
