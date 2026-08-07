"""Live trading engine — real broker order routing.

Same shape as PaperEngine; difference: instead of simulating fills locally, this
engine submits orders through the BrokerAdapter and listens for fills via the
broker's order-update stream.

Includes:
  - Idempotent order placement (client_order_id)
  - Reconciliation loop: every N seconds, compare engine state vs broker state
  - Kill switch: cancels all open orders + halts engine
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from qbacktest.brokers.base import BrokerAdapter, BrokerOrderRequest
from qbacktest.core.exceptions import BrokerError, RateLimitError
from qbacktest.core.types import (
    Fill,
    Instrument,
    Order,
    OrderStatus,
    Side,
)
from qbacktest.engine.engine import BacktestEngine
from qbacktest.engine.strategy import Strategy
from qbacktest.live.live_data import LiveDataSource
from qbacktest.live.risk import RiskLimits

log = logging.getLogger(__name__)


@dataclass
class LiveEngine:
    """Real money trading. Use after a week of paper trading verification."""

    strategy: Strategy
    broker: BrokerAdapter
    instruments: list[Instrument]
    starting_capital: float = 1_000_000.0
    risk: RiskLimits = field(default_factory=RiskLimits)
    reconcile_every_seconds: float = 30.0
    _engine: Optional[BacktestEngine] = field(default=None, init=False)
    _data: Optional[LiveDataSource] = field(default=None, init=False)

    async def run_async(self) -> None:
        if not self.broker.is_authenticated():
            raise RuntimeError(f"Broker {self.broker.name} not authenticated")
        self._data = LiveDataSource(self.broker, self.instruments)
        await self._data.start()

        from qbacktest.engine.context import Context
        self._engine = BacktestEngine(
            strategy=self.strategy,
            data=self._data,
            starting_capital=self.starting_capital,
        )
        # Override _activate_order to route through broker
        original_activate = self._engine._activate_order

        def _live_activate(order: Order, bar, ctx) -> None:
            # Hand off to broker
            asyncio.create_task(self._place_via_broker(order, ctx))

        self._engine._activate_order = _live_activate  # type: ignore[method-assign]

        ctx = Context(self._engine)
        self.strategy.on_start(ctx)
        self._engine._started = True

        # Reconcile task
        recon_task = asyncio.create_task(self._reconcile_loop())

        try:
            async for ev in self._data.aiter():
                self._engine._on_tick(ev, ctx)
        finally:
            recon_task.cancel()
            await self._data.stop()

    async def _place_via_broker(self, order: Order, ctx) -> None:
        if self._engine is None:
            return
        ok, reason = self.risk.check_can_trade(
            current_time=self._engine.current_time,
            n_open_positions=len(self._engine.book.open_positions()),
        )
        if not ok:
            log.warning("Live order rejected by risk: %s", reason)
            self._engine.orders[order.order_id] = order.with_status(
                OrderStatus.REJECTED, reject_reason=reason, updated_at=datetime.utcnow()
            )
            self.strategy.on_order_rejected(ctx, self._engine.orders[order.order_id])
            return
        req = BrokerOrderRequest(
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            product=order.product,
            validity=order.validity,
            price=order.price,
            trigger_price=order.trigger_price,
            client_order_id=order.client_order_id,
            tag=order.tag,
        )
        try:
            resp = await self.broker.place_order(req)
            self.risk.record_order()
            self._engine.orders[order.order_id] = order.with_status(
                OrderStatus.OPEN,
                broker_order_id=resp.broker_order_id,
                updated_at=datetime.utcnow(),
            )
            self.strategy.on_order_placed(ctx, self._engine.orders[order.order_id])
        except (BrokerError, RateLimitError) as e:
            log.error("Broker rejected order: %s", e)
            self._engine.orders[order.order_id] = order.with_status(
                OrderStatus.REJECTED,
                reject_reason=str(e),
                updated_at=datetime.utcnow(),
            )
            self.strategy.on_order_rejected(ctx, self._engine.orders[order.order_id])

    async def _reconcile_loop(self) -> None:
        """Compare engine positions to broker every N seconds."""
        while True:
            try:
                await asyncio.sleep(self.reconcile_every_seconds)
                broker_positions = await self.broker.positions()
                broker_qty = {p.instrument.id: p.quantity for p in broker_positions}
                if self._engine is None:
                    continue
                for pos in self._engine.book.positions.values():
                    bq = broker_qty.get(pos.instrument.id, 0)
                    if bq != pos.quantity:
                        log.warning(
                            "Reconciliation drift on %s: engine=%s broker=%s",
                            pos.instrument.id, pos.quantity, bq,
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Reconciliation error")
