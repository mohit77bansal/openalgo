"""Backtest engine — deterministic event loop.

Drives a Strategy through a stream of bars/ticks, processes orders, fills, and
maintains the position book. Same interface as the live engine; only the data
source differs.

Determinism guarantees:
  - Bars are processed in strict timestamp order.
  - Ties broken by instrument id (lexicographic).
  - Fills are processed before the strategy's next callback fires.
  - No clock-time, no randomness, no thread races.

Same code drives backtest, paper, and live (paper/live live in `qbacktest.live`).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable, Iterator, Optional, Protocol

from qbacktest.core.calendar import EQ_SESSION_CLOSE, EQ_SESSION_OPEN, IST
from qbacktest.core.exceptions import InvalidOrderError
from qbacktest.core.types import (
    Bar,
    Exchange,
    Fill,
    Instrument,
    InstrumentType,
    Order,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
    Side,
    Tick,
    Trade,
)
from qbacktest.costs.costs import CostBreakdown, CostModel, DEFAULT_COST_MODELS
from qbacktest.engine.book import PositionBook
from qbacktest.engine.result import Result
from qbacktest.engine.strategy import Strategy

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data source protocol
# ---------------------------------------------------------------------------


class DataSource(Protocol):
    """A backtest data source yields bars (or ticks) in timestamp order."""

    def stream(self) -> Iterator[Bar | Tick]: ...

    def latest_price(self, instrument: Instrument) -> float | None: ...


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class BacktestEngine:
    """Event-driven backtest engine.

    Parameters
    ----------
    strategy : Strategy
        User strategy.
    data : DataSource
        Source of bars/ticks. Must yield in non-decreasing timestamp order.
    starting_capital : float
        Initial cash, INR.
    cost_model : CostModel | None
        Defaults to Zerodha rates.
    eod_time : time
        When to fire `on_eod`. Default 15:25 IST (5 min before close).
    """

    strategy: Strategy
    data: DataSource
    starting_capital: float = 1_000_000.0
    cost_model: Optional[CostModel] = None
    eod_time: time = time(15, 25)

    # State (initialized in run())
    cash: float = 0.0
    book: PositionBook = field(default_factory=PositionBook)
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fees_breakdown: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    last_prices: dict[str, float] = field(default_factory=dict)

    # Pending orders queued by the strategy this tick — processed at the end of
    # the bar to avoid using future information.
    _pending_submit: list[Order] = field(default_factory=list)
    _pending_cancel: list[str] = field(default_factory=list)

    current_time: datetime = field(default_factory=lambda: datetime.now(IST))
    _last_eod_date: datetime | None = None
    _started: bool = False

    # FIFO trade matcher state — list of open entries per instrument
    _open_lots: dict[str, list[Fill]] = field(default_factory=lambda: defaultdict(list))

    log_records: list[dict[str, object]] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        if self.cost_model is None:
            self.cost_model = DEFAULT_COST_MODELS["zerodha"]
        self.cash = self.starting_capital

    @property
    def equity(self) -> float:
        return self.total_equity

    @property
    def total_equity(self) -> float:
        """Total account equity.

        cash already reflects fills (entry cost has been deducted on buys, premium
        has been credited on sells). To get total equity we add back the *current
        market value* of open positions, signed by direction:

            equity = cash + sum(last_price * quantity)

        For LONG (qty > 0): position market value is +qty * last_price.
        For SHORT (qty < 0): cash already has the proceeds (we *received* premium
        on sell), so the liability is -|qty| * last_price → cash + qty * last_price
        works in both cases since qty is signed.
        """
        market_value = sum(p.last_price * p.quantity for p in self.book.positions.values() if p.quantity != 0)
        return self.cash + market_value

    def last_price(self, instrument: Instrument) -> float | None:
        p = self.last_prices.get(instrument.id)
        if p is not None:
            return p
        return self.data.latest_price(instrument)

    # --- Strategy-callable APIs ---------------------------------------- #

    def submit_order(self, order: Order) -> None:
        """Strategy submits via Context; engine queues for end-of-bar processing."""
        self.orders[order.order_id] = order.with_status(OrderStatus.PENDING)
        self._pending_submit.append(order)

    def cancel_order(self, order_id: str) -> None:
        self._pending_cancel.append(order_id)

    def log(self, msg: str, **fields: object) -> None:
        self.log_records.append({"ts": self.current_time, "msg": msg, **fields})
        log.debug("[%s] %s %s", self.current_time, msg, fields)

    # --- Run ----------------------------------------------------------- #

    def run(self) -> Result:
        from qbacktest.engine.context import Context

        ctx = Context(self)

        if not self._started:
            self.strategy.on_start(ctx)
            self._started = True

        for ev in self.data.stream():
            if self.total_equity <= 0:
                break
            if isinstance(ev, Bar):
                self._on_bar(ev, ctx)
            elif isinstance(ev, Tick):
                self._on_tick(ev, ctx)

        # Final cleanup
        self.strategy.on_finish(ctx)

        # Mark all positions to last known prices (already done) and snapshot.
        return Result(
            strategy_name=self.strategy.__class__.__name__,
            starting_capital=self.starting_capital,
            ending_equity=self.total_equity,
            fills=list(self.fills),
            trades=list(self.trades),
            equity_curve=list(self.equity_curve),
            params=dict(self.strategy.params),
            fees_total=sum(f.fees for f in self.fills),
            fees_breakdown=dict(self.fees_breakdown),
        )

    # --- Bar handling -------------------------------------------------- #

    def _on_bar(self, bar: Bar, ctx) -> None:
        self.current_time = bar.ts
        self.last_prices[bar.instrument.id] = bar.close
        self.book.mark_to_market(bar.instrument, bar.close, bar.ts)

        # Fire session_open / on_eod boundaries
        self._maybe_fire_day_boundaries(ctx)

        # Match working orders against this bar BEFORE strategy callback —
        # otherwise the strategy will see fills from this bar's own data.
        # However this is wrong for MARKET orders submitted on this bar — those
        # should use this bar's close (or next bar's open, depending on convention).
        # We follow "next-bar fill" convention: orders submitted in this bar fill
        # at next bar's open. So match BEFORE callback uses *previous* pending.
        self._match_resting_orders(bar)

        # Strategy reacts
        self.strategy.on_bar(ctx, bar)

        # Queue any new orders submitted in callback for next bar's open
        # (they sit in self.orders with status PENDING, matched on next bar)

        # Cancellations applied immediately
        for oid in self._pending_cancel:
            o = self.orders.get(oid)
            if o is not None and o.is_open:
                self.orders[oid] = o.with_status(OrderStatus.CANCELLED, updated_at=bar.ts)
                self.strategy.on_order_cancelled(ctx, self.orders[oid])
        self._pending_cancel.clear()

        # Newly-submitted orders move from PENDING to OPEN; market orders fill
        # at this bar's close (we use a small simplification: fill at close, not
        # next bar's open, which is fine for end-of-day strategies and minute
        # bars where the difference is tiny).
        for o in self._pending_submit:
            self._activate_order(o, bar, ctx)
        self._pending_submit.clear()

        # Snapshot equity at bar close
        self.equity_curve.append((bar.ts, self.total_equity))

    def _on_tick(self, tick: Tick, ctx) -> None:
        self.current_time = tick.ts
        self.last_prices[tick.instrument.id] = tick.last_price
        self.book.mark_to_market(tick.instrument, tick.last_price, tick.ts)
        self._maybe_fire_day_boundaries(ctx)
        self.strategy.on_tick(ctx, tick)

    # --- Day boundaries ------------------------------------------------ #

    def _maybe_fire_day_boundaries(self, ctx) -> None:
        d = self.current_time.date()
        if self._last_eod_date is None or self._last_eod_date != d:
            # New trading day — fire session_open
            if self.current_time.time() >= EQ_SESSION_OPEN and self._last_eod_date != d:
                # Fire EOD for previous day if exists
                if self._last_eod_date is not None:
                    self.strategy.on_eod(ctx)
                self.strategy.on_session_open(ctx)
                self._last_eod_date = d
        # Trigger on_eod close to session close
        if self.current_time.time() >= self.eod_time and self._last_eod_date == d:
            # Avoid firing twice
            pass  # Engine fires on_eod on the next day's first bar to ensure
                  # all of today's bars/ticks have been processed. Strategies
                  # that need pre-close action should use a time check in on_bar.

    # --- Order matching ------------------------------------------------ #

    def _activate_order(self, order: Order, bar: Bar, ctx) -> None:
        """Convert PENDING → OPEN, optionally fill immediately for MARKET."""
        # Validate
        try:
            self._validate(order)
        except InvalidOrderError as e:
            rejected = order.with_status(OrderStatus.REJECTED, reject_reason=str(e), updated_at=bar.ts)
            self.orders[order.order_id] = rejected
            self.strategy.on_order_rejected(ctx, rejected)
            return

        # Move to OPEN
        active = order.with_status(OrderStatus.OPEN, updated_at=bar.ts)
        self.orders[order.order_id] = active
        self.strategy.on_order_placed(ctx, active)

        # Market orders fill at this bar's close (with slippage)
        if order.order_type == OrderType.MARKET and order.instrument.id == bar.instrument.id:
            self._fill_at(active, bar.close, bar.ts, ctx)

    def _match_resting_orders(self, bar: Bar) -> None:
        """Match LIMIT and SL orders against this bar's range."""
        for oid, order in list(self.orders.items()):
            if not order.is_open:
                continue
            if order.instrument.id != bar.instrument.id:
                continue
            fill_px = self._maybe_match(order, bar)
            if fill_px is not None:
                # Find a context — we don't have it here cleanly, build a temp
                from qbacktest.engine.context import Context
                ctx = Context(self)
                self._fill_at(order, fill_px, bar.ts, ctx)

    def _maybe_match(self, order: Order, bar: Bar) -> float | None:
        if order.order_type == OrderType.LIMIT and order.price is not None:
            if order.side == Side.BUY and bar.low <= order.price:
                return order.price
            if order.side == Side.SELL and bar.high >= order.price:
                return order.price
        elif order.order_type in (OrderType.SL, OrderType.SL_M) and order.trigger_price is not None:
            if order.side == Side.BUY and bar.high >= order.trigger_price:
                return order.trigger_price if order.order_type == OrderType.SL_M else (order.price or order.trigger_price)
            if order.side == Side.SELL and bar.low <= order.trigger_price:
                return order.trigger_price if order.order_type == OrderType.SL_M else (order.price or order.trigger_price)
        return None

    def _validate(self, order: Order) -> None:
        if order.quantity <= 0:
            raise InvalidOrderError("Quantity must be > 0")
        if order.order_type in (OrderType.LIMIT, OrderType.SL) and order.price is None:
            raise InvalidOrderError(f"{order.order_type} requires price")
        if order.order_type in (OrderType.SL, OrderType.SL_M) and order.trigger_price is None:
            raise InvalidOrderError(f"{order.order_type} requires trigger price")
        # Lot-size multiple check for derivatives
        if order.instrument.is_derivative:
            ls = order.instrument.lot_size
            if ls > 0 and order.quantity % ls != 0:
                raise InvalidOrderError(
                    f"Order qty {order.quantity} not a multiple of lot size {ls} "
                    f"for {order.instrument.id}"
                )

    # --- Fills --------------------------------------------------------- #

    def _fill_at(self, order: Order, raw_price: float, ts: datetime, ctx) -> None:
        # Slippage
        assert self.cost_model is not None
        adj_price = self.cost_model.apply_slippage(
            side=order.side,
            price=raw_price,
            quantity=order.quantity,
            lot_size=max(order.instrument.lot_size, 1),
        )
        cost = self.cost_model.compute_fill(
            exchange=order.instrument.exchange,
            instrument_type=order.instrument.instrument_type,
            product=order.product,
            side=order.side,
            quantity=order.quantity,
            price=adj_price,
            trade_date=ts.date(),
        )
        fill = Fill(
            fill_id=str(uuid.uuid4()),
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=adj_price,
            ts=ts,
            fees=cost.total,
            cost_breakdown=cost.as_dict(),
        )
        self.fills.append(fill)

        # Cash impact: buy reduces cash, sell increases. Fees always reduce.
        signed_cash_flow = -fill.signed_quantity * fill.price - fill.fees
        self.cash += signed_cash_flow

        # Apply to position book
        realized = self.book.apply_fill(fill)

        # Aggregate fees for breakdown
        for k, v in cost.as_dict().items():
            if k == "total":
                continue
            self.fees_breakdown[k] = self.fees_breakdown.get(k, 0.0) + float(v)

        # Update order
        new_filled = order.filled_qty + fill.quantity
        new_status = OrderStatus.FILLED if new_filled >= order.quantity else OrderStatus.PARTIALLY_FILLED
        # VWAP avg
        prior_value = order.avg_fill_price * order.filled_qty
        new_avg = (prior_value + fill.price * fill.quantity) / new_filled if new_filled > 0 else 0.0
        self.orders[order.order_id] = order.with_status(
            new_status,
            filled_qty=new_filled,
            avg_fill_price=new_avg,
            updated_at=ts,
        )

        # Match trade (FIFO)
        self._record_trade(fill)

        self.strategy.on_fill(ctx, fill)

    def _record_trade(self, fill: Fill) -> None:
        """FIFO match closes against opens to produce Trade records."""
        bucket = self._open_lots[fill.instrument.id]
        # If the new fill is opposing existing open lots, match FIFO
        if bucket and bucket[0].side != fill.side:
            remaining = fill.quantity
            while remaining > 0 and bucket and bucket[0].side != fill.side:
                opener = bucket[0]
                close_qty = min(opener.quantity, remaining)
                # Build matched trade — use fractional fills if needed
                if close_qty == opener.quantity:
                    bucket.pop(0)
                else:
                    # Reduce opener qty in place by replacing with a smaller lot
                    bucket[0] = Fill(
                        fill_id=opener.fill_id,
                        order_id=opener.order_id,
                        instrument=opener.instrument,
                        side=opener.side,
                        quantity=opener.quantity - close_qty,
                        price=opener.price,
                        ts=opener.ts,
                        fees=opener.fees * (opener.quantity - close_qty) / opener.quantity,
                    )
                exit_qty_fill = Fill(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    instrument=fill.instrument,
                    side=fill.side,
                    quantity=close_qty,
                    price=fill.price,
                    ts=fill.ts,
                    fees=fill.fees * close_qty / fill.quantity,
                )
                opener_lot = Fill(
                    fill_id=opener.fill_id,
                    order_id=opener.order_id,
                    instrument=opener.instrument,
                    side=opener.side,
                    quantity=close_qty,
                    price=opener.price,
                    ts=opener.ts,
                    fees=opener.fees * close_qty / opener.quantity,
                )
                self.trades.append(Trade(
                    trade_id=str(uuid.uuid4()),
                    instrument=fill.instrument,
                    entry_fill=opener_lot,
                    exit_fill=exit_qty_fill,
                    direction=opener.side,
                ))
                remaining -= close_qty
            # Any leftover opens a new lot in the opposite direction
            if remaining > 0:
                bucket.append(Fill(
                    fill_id=fill.fill_id + "-rem",
                    order_id=fill.order_id,
                    instrument=fill.instrument,
                    side=fill.side,
                    quantity=remaining,
                    price=fill.price,
                    ts=fill.ts,
                    fees=0.0,  # already counted on closed portion
                ))
        else:
            # Opening lot
            bucket.append(fill)
