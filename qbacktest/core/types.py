"""Core domain types — instruments, orders, fills, positions, ticks, bars.

All types are frozen dataclasses (immutable). Mutation is done by replace(), never
in-place. This is critical for backtest determinism and for safe concurrency in the
optimizer's worker pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE F&O
    BFO = "BFO"  # BSE F&O
    MCX = "MCX"  # commodity (future)
    CDS = "CDS"  # currency (future)


class InstrumentType(str, Enum):
    EQ = "EQ"
    INDEX = "INDEX"
    FUT = "FUT"
    CE = "CE"  # call option
    PE = "PE"  # put option

    @property
    def is_option(self) -> bool:
        return self in (InstrumentType.CE, InstrumentType.PE)

    @property
    def is_derivative(self) -> bool:
        return self in (InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE)


class OptionType(str, Enum):
    CALL = "CE"
    PUT = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"           # stop-loss with limit
    SL_M = "SL_M"       # stop-loss market


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"  # immediate or cancel
    GTC = "GTC"  # good till cancelled (broker may not support)


class ProductType(str, Enum):
    """Indian broker product types."""
    INTRADAY = "MIS"   # margin intraday square-off
    DELIVERY = "CNC"   # cash and carry
    NRML = "NRML"      # normal F&O carry-forward
    COVER = "CO"       # cover order
    BRACKET = "BO"     # bracket order


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable instrument.

    Canonical id format:
      Equity:  "NSE:RELIANCE-EQ"
      Index:   "NSE:NIFTY"
      Future:  "NFO:NIFTY24DECFUT"
      Option:  "NFO:NIFTY24DEC25000CE"
    """

    symbol: str                # e.g. "NIFTY", "RELIANCE"
    exchange: Exchange
    instrument_type: InstrumentType
    lot_size: int = 1          # 1 for equity, lot size for F&O
    tick_size: float = 0.05    # price increment
    # Derivative-only fields:
    expiry: Optional[date] = None
    strike: Optional[float] = None       # options only
    underlying: Optional[str] = None     # e.g. "NIFTY" for "NIFTY24DEC25000CE"
    # Optional: broker-specific instrument key (e.g. Upstox NSE_FO|123456)
    broker_keys: dict[str, str] = field(default_factory=dict, hash=False)

    @property
    def id(self) -> str:
        if self.instrument_type == InstrumentType.EQ:
            return f"{self.exchange.value}:{self.symbol}-EQ"
        if self.instrument_type == InstrumentType.INDEX:
            return f"{self.exchange.value}:{self.symbol}"
        if self.instrument_type == InstrumentType.FUT:
            assert self.expiry is not None
            return f"{self.exchange.value}:{self.symbol}{self.expiry:%y%b}FUT".upper()
        # option
        assert self.expiry is not None and self.strike is not None
        strike_part = f"{int(self.strike)}" if float(self.strike).is_integer() else f"{self.strike:.2f}"
        return f"{self.exchange.value}:{self.symbol}{self.expiry:%y%b}{strike_part}{self.instrument_type.value}".upper()

    @property
    def is_option(self) -> bool:
        return self.instrument_type.is_option

    @property
    def is_derivative(self) -> bool:
        return self.instrument_type.is_derivative


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tick:
    """A single quote/trade tick."""
    instrument: Instrument
    ts: datetime
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar."""
    instrument: Instrument
    ts: datetime              # bar OPEN time (canonical)
    timeframe: str            # e.g. "1m", "5m", "1d"
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    open_interest: Optional[int] = None

    @property
    def hl2(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def hlc3(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def ohlc4(self) -> float:
        return (self.open + self.high + self.low + self.close) / 4.0


# ---------------------------------------------------------------------------
# Orders, fills, positions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Order:
    """A trading order. Immutable; mutate via dataclasses.replace()."""
    order_id: str
    instrument: Instrument
    side: Side
    quantity: int                          # in units (lots × lot_size)
    order_type: OrderType
    price: Optional[float] = None          # required for LIMIT, SL
    trigger_price: Optional[float] = None  # required for SL, SL_M
    product: ProductType = ProductType.NRML
    validity: Validity = Validity.DAY
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    placed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Strategy-supplied tag for grouping multi-leg orders, attribution, etc.
    tag: Optional[str] = None
    # Broker order id (set after placement)
    broker_order_id: Optional[str] = None
    # Idempotency key for retries
    client_order_id: Optional[str] = None
    # If this order is part of a multi-leg construct
    multi_leg_id: Optional[str] = None
    # Reject reason (only populated when status == REJECTED)
    reject_reason: Optional[str] = None

    @property
    def lots(self) -> int:
        return self.quantity // self.instrument.lot_size

    @property
    def is_buy(self) -> bool:
        return self.side == Side.BUY

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal

    @property
    def remaining_qty(self) -> int:
        return self.quantity - self.filled_qty

    def with_status(self, status: OrderStatus, **kwargs: object) -> Order:
        return replace(self, status=status, **kwargs)


@dataclass(frozen=True, slots=True)
class Fill:
    """An execution against an order."""
    fill_id: str
    order_id: str
    instrument: Instrument
    side: Side
    quantity: int
    price: float
    ts: datetime
    fees: float = 0.0           # all costs (brokerage + STT + exch + GST + stamp)
    cost_breakdown: dict[str, float] = field(default_factory=dict, hash=False)

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    @property
    def signed_quantity(self) -> int:
        return self.quantity * self.side.sign


@dataclass(frozen=True, slots=True)
class Trade:
    """A round-trip trade (entry fill matched with exit fill).

    For accounting. Realized PnL is on exit only.
    """
    trade_id: str
    instrument: Instrument
    entry_fill: Fill
    exit_fill: Fill
    # entry side (LONG = bought to open; SHORT = sold to open)
    direction: Side

    @property
    def gross_pnl(self) -> float:
        # For LONG: (exit - entry) * qty
        # For SHORT: (entry - exit) * qty
        sign = 1 if self.direction == Side.BUY else -1
        qty = min(self.entry_fill.quantity, self.exit_fill.quantity)
        return sign * (self.exit_fill.price - self.entry_fill.price) * qty

    @property
    def fees(self) -> float:
        return self.entry_fill.fees + self.exit_fill.fees

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def duration_seconds(self) -> float:
        return (self.exit_fill.ts - self.entry_fill.ts).total_seconds()


@dataclass(frozen=True, slots=True)
class Position:
    """Net position in a single instrument.

    For a backtest, the engine maintains one Position per instrument across all
    strategies. Quantity is signed: positive = LONG, negative = SHORT.
    """
    instrument: Instrument
    quantity: int = 0           # signed, in units
    avg_price: float = 0.0      # volume-weighted avg cost basis
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_price: float = 0.0     # mark price
    opened_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def lots(self) -> int:
        return abs(self.quantity) // self.instrument.lot_size * (1 if self.is_long else -1)

    @property
    def market_value(self) -> float:
        return self.last_price * self.quantity

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def mark(self, last_price: float, ts: datetime) -> Position:
        """Update mark price; recompute unrealized PnL."""
        unrealized = (last_price - self.avg_price) * self.quantity if self.quantity != 0 else 0.0
        return replace(self, last_price=last_price, unrealized_pnl=unrealized, last_updated=ts)

    def apply_fill(self, fill: Fill) -> tuple[Position, float]:
        """Apply a fill; return (new_position, realized_pnl_from_this_fill).

        Handles long/short flipping, partial close, full close, fresh open.
        """
        signed_qty = fill.signed_quantity
        new_qty = self.quantity + signed_qty
        realized = 0.0

        if self.quantity == 0:
            # Fresh open
            new_avg = fill.price
        elif (self.quantity > 0 and signed_qty > 0) or (self.quantity < 0 and signed_qty < 0):
            # Adding to existing position — VWAP avg
            total_cost = self.avg_price * abs(self.quantity) + fill.price * abs(signed_qty)
            new_avg = total_cost / abs(new_qty)
        else:
            # Reducing or flipping
            close_qty = min(abs(signed_qty), abs(self.quantity))
            # PnL on closed portion (sign convention: long realizes (exit - entry) * qty)
            direction = 1 if self.quantity > 0 else -1
            realized = direction * (fill.price - self.avg_price) * close_qty
            if abs(signed_qty) <= abs(self.quantity):
                # Reduce only
                new_avg = self.avg_price if new_qty != 0 else 0.0
            else:
                # Flipped — new avg is fill price for the remaining qty
                new_avg = fill.price

        if new_qty == 0:
            new_avg = 0.0

        return (
            replace(
                self,
                quantity=new_qty,
                avg_price=new_avg,
                realized_pnl=self.realized_pnl + realized - fill.fees,
                last_price=fill.price,
                opened_at=self.opened_at if self.quantity != 0 else fill.ts,
                last_updated=fill.ts,
            ),
            realized - fill.fees,
        )


# ---------------------------------------------------------------------------
# Multi-leg
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MultiLegOrder:
    """A multi-leg construct (spread, condor, butterfly, straddle).

    The engine submits each leg as its own Order, but groups them under a shared
    multi_leg_id for atomic cost/margin calculation and atomic cancellation.
    """
    multi_leg_id: str
    legs: tuple[Order, ...]
    construct: str           # "VERTICAL", "STRADDLE", "STRANGLE", "IRON_CONDOR", "BUTTERFLY", "CALENDAR", "DIAGONAL", "CUSTOM"
    tag: Optional[str] = None

    def with_legs(self, legs: tuple[Order, ...]) -> MultiLegOrder:
        return replace(self, legs=legs)
