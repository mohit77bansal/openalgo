"""Broker-agnostic interfaces.

Both Upstox and Fyers (and future broker integrations) implement this surface.
The live engine talks to BrokerAdapter; nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import AsyncIterator, Awaitable, Callable, Optional

from qbacktest.core.types import (
    Bar,
    Exchange,
    Instrument,
    Order,
    OrderType,
    Position,
    ProductType,
    Side,
    Tick,
    Validity,
)


# ---------------------------------------------------------------------------
# Broker DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerOrderRequest:
    """Normalized order request the adapter will translate to broker-specific JSON."""
    instrument: Instrument
    side: Side
    quantity: int
    order_type: OrderType
    product: ProductType
    validity: Validity = Validity.DAY
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    client_order_id: Optional[str] = None
    tag: Optional[str] = None


@dataclass(frozen=True, slots=True)
class BrokerOrderResponse:
    """Adapter's reply after submitting an order."""
    broker_order_id: str
    client_order_id: Optional[str]
    status: str            # broker-native; we map back to OrderStatus in the live engine
    accepted_at: datetime
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """Snapshot of a broker-side position."""
    instrument: Instrument
    quantity: int          # signed, units
    avg_price: float
    last_price: float
    realized_pnl: float
    unrealized_pnl: float
    product: ProductType


@dataclass(frozen=True, slots=True)
class LiveQuote:
    """A single live tick/quote from the broker websocket."""
    instrument: Instrument
    ts: datetime
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BrokerAdapter(ABC):
    """Abstract interface every broker integration implements."""

    name: str = "abstract"

    # --- Auth --------------------------------------------------------------- #

    @abstractmethod
    def authorize_url(self) -> str:
        """Return the OAuth authorize URL the user opens in their browser."""

    @abstractmethod
    async def exchange_code_for_token(self, code: str) -> dict[str, object]:
        """Complete OAuth: trade the redirect `code` for an access token."""

    @abstractmethod
    async def refresh_token(self) -> dict[str, object]:
        """Refresh the access token (if broker supports refresh tokens)."""

    @abstractmethod
    def is_authenticated(self) -> bool: ...

    # --- Reference data ----------------------------------------------------- #

    @abstractmethod
    async def fetch_symbol_master(self) -> list[Instrument]:
        """Pull the broker's full instrument master."""

    # --- Historical --------------------------------------------------------- #

    @abstractmethod
    async def historical_candles(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        timeframe: str = "1d",
    ) -> list[Bar]:
        """Fetch historical OHLC bars."""

    # --- Live data ---------------------------------------------------------- #

    @abstractmethod
    async def subscribe_quotes(
        self,
        instruments: list[Instrument],
        on_quote: Callable[[LiveQuote], Awaitable[None]],
    ) -> None:
        """Subscribe to live ticks; invokes callback per quote."""

    @abstractmethod
    async def unsubscribe(self, instruments: list[Instrument]) -> None: ...

    # --- Orders ------------------------------------------------------------- #

    @abstractmethod
    async def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResponse: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> BrokerOrderResponse: ...

    # --- Account ------------------------------------------------------------ #

    @abstractmethod
    async def positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def holdings(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def funds(self) -> dict[str, float]: ...
