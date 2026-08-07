"""Option chain snapshot — atomic view of all strikes for an underlying+expiry at time T.

Arbitrage scanners (box, parity, adjacent-strike) need to query the full chain at a
single moment, not stream tick-by-tick. This type captures that view immutably.

Used by:
- `qbacktest.strategies.arbitrage.*` scanners
- The Web UI's arbitrage-opportunity page
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from qbacktest.core.types import Instrument, InstrumentType, OptionType


@dataclass(frozen=True, slots=True)
class ChainQuote:
    """Single (strike, type) quote within a chain snapshot."""

    strike: float
    option_type: OptionType        # CALL or PUT
    last_price: float              # last traded
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    iv: Optional[float] = None     # implied vol if computed

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None and self.ask > self.bid:
            return (self.bid + self.ask) / 2.0
        return self.last_price

    @property
    def spread_bps(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.bid <= 0:
            return None
        return (self.ask - self.bid) / self.mid * 10_000

    @property
    def spread_inr(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """All option quotes for one underlying + one expiry at one timestamp."""

    underlying: str
    expiry: date
    spot: float                          # underlying spot at snapshot time
    ts: datetime
    risk_free_rate: float                # decimal, e.g. 0.07
    quotes: tuple[ChainQuote, ...] = field(default_factory=tuple)
    lot_size: int = 1
    futures_price: Optional[float] = None

    @property
    def time_to_expiry(self) -> float:
        """Years to expiry (calendar; for trading-days adjustment do externally)."""
        return max((self.expiry - self.ts.date()).days / 365.0, 0.0)

    @property
    def strikes(self) -> list[float]:
        return sorted({q.strike for q in self.quotes})

    def get(self, strike: float, option_type: OptionType) -> Optional[ChainQuote]:
        for q in self.quotes:
            if q.strike == strike and q.option_type == option_type:
                return q
        return None

    def call(self, strike: float) -> Optional[ChainQuote]:
        return self.get(strike, OptionType.CALL)

    def put(self, strike: float) -> Optional[ChainQuote]:
        return self.get(strike, OptionType.PUT)

    def calls(self) -> list[ChainQuote]:
        return sorted([q for q in self.quotes if q.option_type == OptionType.CALL], key=lambda q: q.strike)

    def puts(self) -> list[ChainQuote]:
        return sorted([q for q in self.quotes if q.option_type == OptionType.PUT], key=lambda q: q.strike)

    def atm_strike(self, *, step: int = 50) -> float:
        return round(self.spot / step) * step

    def make_instrument(self, strike: float, option_type: OptionType) -> Instrument:
        from qbacktest.core.types import Exchange

        return Instrument(
            symbol=self.underlying,
            exchange=Exchange.NFO,
            instrument_type=InstrumentType.CE if option_type == OptionType.CALL else InstrumentType.PE,
            lot_size=self.lot_size,
            tick_size=0.05,
            expiry=self.expiry,
            strike=strike,
            underlying=self.underlying,
        )
