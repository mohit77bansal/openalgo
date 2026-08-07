"""Paper trading engine.

Live data, simulated fills. Uses the L1 quote from the broker to fill orders at
mid (or best bid/ask if available), with the same cost model as backtest. Used as
mandatory pre-live validation: trader runs strategy in paper mode for at least a
week, compares paper PnL vs engine-predicted PnL, before going live.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from qbacktest.brokers.base import BrokerAdapter
from qbacktest.core.types import Fill, Instrument, Order, OrderStatus, Side
from qbacktest.engine.engine import BacktestEngine
from qbacktest.engine.strategy import Strategy
from qbacktest.live.live_data import LiveDataSource
from qbacktest.live.risk import RiskLimits

log = logging.getLogger(__name__)


@dataclass
class PaperEngine:
    """Live data, fake fills.

    Wraps BacktestEngine — same engine code, same cost model. The only difference:
    data comes from the broker's WebSocket instead of Parquet.
    """

    strategy: Strategy
    broker: BrokerAdapter
    instruments: list[Instrument]
    starting_capital: float = 1_000_000.0
    risk: RiskLimits = field(default_factory=RiskLimits)
    _engine: Optional[BacktestEngine] = field(default=None, init=False)
    _data: Optional[LiveDataSource] = field(default=None, init=False)

    async def run_async(self) -> None:
        # Build a LiveDataSource. The engine in paper mode uses tick-level events.
        self._data = LiveDataSource(self.broker, self.instruments)
        # Authenticate
        if not self.broker.is_authenticated():
            raise RuntimeError(f"Broker {self.broker.name} not authenticated")
        # Start feed
        await self._data.start()

        # Construct an engine. We can't call engine.run() (it's sync); we drive it
        # one tick at a time via run_one_event.
        from qbacktest.engine.context import Context
        self._engine = BacktestEngine(
            strategy=self.strategy,
            data=self._data,
            starting_capital=self.starting_capital,
        )
        ctx = Context(self._engine)
        self._engine._started = True
        self.strategy.on_start(ctx)

        try:
            async for ev in self._data.aiter():
                # Engine processing per tick
                self._engine._on_tick(ev, ctx)
                # Risk check loop
                ok, reason = self.risk.check_can_trade(
                    current_time=self._engine.current_time,
                    n_open_positions=len(self._engine.book.open_positions()),
                )
                if not ok and any(o.is_open for o in self._engine.orders.values()):
                    log.warning("Risk halt: %s — cancelling open orders", reason)
                    for o in list(self._engine.orders.values()):
                        if o.is_open:
                            self._engine.cancel_order(o.order_id)
        finally:
            await self._data.stop()

    def stop(self) -> None:
        if self._data is not None:
            asyncio.create_task(self._data.stop())
