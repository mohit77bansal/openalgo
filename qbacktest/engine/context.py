"""Context — runtime API exposed to strategies.

The engine constructs a `Context` and passes it to strategy callbacks. It abstracts
data, order placement, and queries so the same strategy code runs in backtest,
paper, and live modes.

Strategies should NEVER mutate context state directly — they call methods that the
engine intercepts and processes deterministically.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from qbacktest.core.calendar import DEFAULT_CALENDAR, TradingCalendar
from qbacktest.core.lot_sizes import lot_size_on
from qbacktest.core.types import (
    Exchange,
    Instrument,
    InstrumentType,
    Order,
    OrderType,
    Position,
    ProductType,
    Side,
    Validity,
)

if TYPE_CHECKING:
    from qbacktest.engine.engine import BacktestEngine


class Context:
    """Runtime API. The engine injects this into strategy callbacks."""

    def __init__(self, engine: BacktestEngine) -> None:
        self._engine = engine
        self.calendar: TradingCalendar = DEFAULT_CALENDAR

    # --- Time / state ---------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._engine.current_time

    @property
    def today(self) -> date:
        return self._engine.current_time.date()

    @property
    def cash(self) -> float:
        return self._engine.cash

    @property
    def equity(self) -> float:
        """Total account equity = cash + sum(market value of positions)."""
        return self._engine.equity

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._engine.book.positions)

    def position(self, instrument_or_id: Instrument | str) -> Optional[Position]:
        if isinstance(instrument_or_id, Instrument):
            return self._engine.book.positions.get(instrument_or_id.id)
        return self._engine.book.positions.get(instrument_or_id)

    @property
    def open_orders(self) -> list[Order]:
        return [o for o in self._engine.orders.values() if o.is_open]

    # --- Instrument helpers ---------------------------------------------------

    def index(self, symbol: str, exchange: Exchange = Exchange.NSE) -> Instrument:
        return Instrument(
            symbol=symbol.upper(),
            exchange=exchange,
            instrument_type=InstrumentType.INDEX,
            lot_size=1,
        )

    def equity_(self, symbol: str, exchange: Exchange = Exchange.NSE) -> Instrument:
        return Instrument(
            symbol=symbol.upper(),
            exchange=exchange,
            instrument_type=InstrumentType.EQ,
            lot_size=1,
        )

    def future(self, underlying: str, expiry: date, exchange: Exchange = Exchange.NFO) -> Instrument:
        return Instrument(
            symbol=underlying.upper(),
            exchange=exchange,
            instrument_type=InstrumentType.FUT,
            lot_size=lot_size_on(underlying, expiry),
            expiry=expiry,
            underlying=underlying.upper(),
        )

    def option(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        exchange: Exchange = Exchange.NFO,
    ) -> Instrument:
        ot = option_type.upper()
        if ot in ("CALL", "C"):
            ot = "CE"
        elif ot in ("PUT", "P"):
            ot = "PE"
        if ot not in ("CE", "PE"):
            raise ValueError(f"Bad option type {option_type!r}")
        return Instrument(
            symbol=underlying.upper(),
            exchange=exchange,
            instrument_type=InstrumentType.CE if ot == "CE" else InstrumentType.PE,
            lot_size=lot_size_on(underlying, expiry),
            tick_size=0.05,
            expiry=expiry,
            strike=float(strike),
            underlying=underlying.upper(),
        )

    # --- Calendar / expiry helpers --------------------------------------------

    def next_weekly_expiry(self, underlying: str, after: Optional[date] = None) -> date:
        d = after or self.today
        e = self.calendar.next_weekly_expiry(underlying, d)
        if e is None:
            raise ValueError(f"{underlying} has no weekly expiry on/after {d}")
        return e

    def monthly_expiry(self, underlying: str, year: int, month: int) -> date | None:
        return self.calendar.monthly_expiry(underlying, year, month)

    def atm_strike(self, underlying: str, expiry: date, *, step: int = 50) -> float:
        """ATM strike for the underlying at current spot, rounded to step.

        Default step (50) works for NIFTY. Use 100 for BANKNIFTY, 25 for stocks.
        Override in strategy code if you need different rounding.
        """
        spot = self._engine.last_price(self.index(underlying))
        if spot is None:
            raise RuntimeError(f"No spot price for {underlying} at {self.now}")
        return round(spot / step) * step

    # --- Order placement ------------------------------------------------------

    def buy(
        self,
        instrument: Instrument,
        *,
        lots: int = 1,
        quantity: int | None = None,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        product: ProductType = ProductType.NRML,
        tag: str | None = None,
    ) -> Order:
        return self._submit(
            instrument=instrument,
            side=Side.BUY,
            lots=lots,
            quantity=quantity,
            order_type=order_type,
            price=price,
            product=product,
            tag=tag,
        )

    def sell(
        self,
        instrument: Instrument,
        *,
        lots: int = 1,
        quantity: int | None = None,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        product: ProductType = ProductType.NRML,
        tag: str | None = None,
    ) -> Order:
        return self._submit(
            instrument=instrument,
            side=Side.SELL,
            lots=lots,
            quantity=quantity,
            order_type=order_type,
            price=price,
            product=product,
            tag=tag,
        )

    def buy_option(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        *,
        lots: int = 1,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        tag: str | None = None,
    ) -> Order:
        return self.buy(
            self.option(underlying, expiry, strike, option_type),
            lots=lots,
            order_type=order_type,
            price=price,
            tag=tag,
        )

    def sell_option(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        *,
        lots: int = 1,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        tag: str | None = None,
    ) -> Order:
        return self.sell(
            self.option(underlying, expiry, strike, option_type),
            lots=lots,
            order_type=order_type,
            price=price,
            tag=tag,
        )

    def cancel(self, order: Order) -> None:
        self._engine.cancel_order(order.order_id)

    def square_off(self, instrument: Instrument) -> Order | None:
        """Close a position by submitting an opposite market order."""
        pos = self.position(instrument)
        if pos is None or pos.is_flat:
            return None
        return self._submit(
            instrument=instrument,
            side=Side.SELL if pos.is_long else Side.BUY,
            lots=None,
            quantity=abs(pos.quantity),
            order_type=OrderType.MARKET,
            price=None,
            product=ProductType.NRML,
            tag="square-off",
        )

    def square_off_all(self) -> list[Order]:
        """Square off every open position."""
        out = []
        for pos in list(self._engine.book.positions.values()):
            if pos.is_flat:
                continue
            o = self.square_off(pos.instrument)
            if o is not None:
                out.append(o)
        return out

    # --- Internal submit ------------------------------------------------------

    def _submit(
        self,
        *,
        instrument: Instrument,
        side: Side,
        lots: int | None,
        quantity: int | None,
        order_type: OrderType,
        price: float | None,
        product: ProductType,
        tag: str | None,
    ) -> Order:
        if quantity is None:
            assert lots is not None, "Either lots or quantity is required"
            quantity = lots * max(instrument.lot_size, 1)
        order = Order(
            order_id=str(uuid.uuid4()),
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            product=product,
            validity=Validity.DAY,
            placed_at=self.now,
            tag=tag,
            client_order_id=f"qb-{uuid.uuid4().hex[:12]}",
        )
        self._engine.submit_order(order)
        return order

    # --- Logging --------------------------------------------------------------

    def log(self, msg: str, **fields: Any) -> None:
        self._engine.log(msg, **fields)
