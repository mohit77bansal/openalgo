"""Live chain-snapshot builder.

Subscribes to all option strikes for an underlying via a `BrokerAdapter`,
maintains the latest quote per (strike, type), and reconstructs a `ChainSnapshot`
on demand or every N seconds.

Usage:

    builder = ChainBuilder(adapter, underlying="NIFTY", expiry=date(2024, 12, 12))
    await builder.start()
    # later, in another task or via callback:
    snap = builder.snapshot()  # latest reconstructed snapshot
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from qbacktest.brokers.base import BrokerAdapter, LiveQuote
from qbacktest.core.calendar import IST
from qbacktest.core.chain import ChainQuote, ChainSnapshot
from qbacktest.core.lot_sizes import lot_size_on
from qbacktest.core.types import Exchange, Instrument, InstrumentType, OptionType

log = logging.getLogger(__name__)


@dataclass
class ChainBuilder:
    """Maintains a rolling option chain snapshot from a broker live feed."""

    broker: BrokerAdapter
    underlying: str
    expiry: date
    strikes: list[float]                          # which strikes to track
    risk_free_rate: float = 0.07
    spot_instrument: Optional[Instrument] = None  # for spot price
    futures_instrument: Optional[Instrument] = None
    _latest_quotes: dict[tuple[float, OptionType], ChainQuote] = field(default_factory=dict, init=False)
    _spot_price: Optional[float] = field(default=None, init=False)
    _futures_price: Optional[float] = field(default=None, init=False)
    _lot_size: int = field(init=False)

    def __post_init__(self) -> None:
        self._lot_size = lot_size_on(self.underlying, self.expiry)

    def _build_instruments(self) -> list[Instrument]:
        out: list[Instrument] = []
        for k in self.strikes:
            out.append(Instrument(
                symbol=self.underlying,
                exchange=Exchange.NFO,
                instrument_type=InstrumentType.CE,
                lot_size=self._lot_size,
                tick_size=0.05,
                expiry=self.expiry,
                strike=k,
                underlying=self.underlying,
            ))
            out.append(Instrument(
                symbol=self.underlying,
                exchange=Exchange.NFO,
                instrument_type=InstrumentType.PE,
                lot_size=self._lot_size,
                tick_size=0.05,
                expiry=self.expiry,
                strike=k,
                underlying=self.underlying,
            ))
        if self.spot_instrument:
            out.append(self.spot_instrument)
        if self.futures_instrument:
            out.append(self.futures_instrument)
        return out

    async def start(self) -> None:
        if not self.broker.is_authenticated():
            raise RuntimeError(f"Broker {self.broker.name} not authenticated")
        instruments = self._build_instruments()
        await self.broker.subscribe_quotes(instruments, self._on_quote)

    async def stop(self) -> None:
        instruments = self._build_instruments()
        await self.broker.unsubscribe(instruments)

    async def _on_quote(self, quote: LiveQuote) -> None:
        inst = quote.instrument
        if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE) and inst.strike is not None:
            ot = OptionType.CALL if inst.instrument_type == InstrumentType.CE else OptionType.PUT
            self._latest_quotes[(inst.strike, ot)] = ChainQuote(
                strike=inst.strike,
                option_type=ot,
                last_price=quote.last_price,
                bid=quote.bid,
                ask=quote.ask,
                volume=quote.volume or 0,
                open_interest=quote.open_interest or 0,
            )
        elif inst.instrument_type == InstrumentType.INDEX:
            self._spot_price = quote.last_price
        elif inst.instrument_type == InstrumentType.FUT:
            self._futures_price = quote.last_price

    def snapshot(self) -> Optional[ChainSnapshot]:
        if not self._latest_quotes or self._spot_price is None:
            return None
        return ChainSnapshot(
            underlying=self.underlying,
            expiry=self.expiry,
            spot=self._spot_price,
            ts=datetime.now(IST),
            risk_free_rate=self.risk_free_rate,
            quotes=tuple(self._latest_quotes.values()),
            lot_size=self._lot_size,
            futures_price=self._futures_price,
        )
