"""Live data source adapter.

Bridges a broker WebSocket feed to the engine's `DataSource` protocol. Translates
LiveQuote into Tick (or aggregates ticks into Bars).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterator

from qbacktest.brokers.base import BrokerAdapter, LiveQuote
from qbacktest.core.types import Bar, Instrument, Tick

log = logging.getLogger(__name__)


@dataclass
class LiveDataSource:
    """Async live feed that yields Ticks/Bars to the engine.

    Usage (consumer side):
        async with LiveDataSource(broker, instruments) as src:
            async for ev in src.aiter():
                ...

    For the synchronous engine, we use a queue and an asyncio→sync bridge.
    """
    broker: BrokerAdapter
    instruments: list[Instrument]
    _queue: asyncio.Queue[LiveQuote] = field(default_factory=asyncio.Queue, init=False)
    _last_price: dict[str, float] = field(default_factory=dict, init=False)
    _running: bool = field(default=False, init=False)

    async def start(self) -> None:
        self._running = True
        await self.broker.subscribe_quotes(self.instruments, self._on_quote)

    async def stop(self) -> None:
        self._running = False
        await self.broker.unsubscribe(self.instruments)

    async def _on_quote(self, quote: LiveQuote) -> None:
        self._last_price[quote.instrument.id] = quote.last_price
        await self._queue.put(quote)

    async def aiter(self):
        while self._running or not self._queue.empty():
            try:
                quote = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield Tick(
                instrument=quote.instrument,
                ts=quote.ts,
                last_price=quote.last_price,
                bid=quote.bid,
                ask=quote.ask,
                bid_qty=quote.bid_qty,
                ask_qty=quote.ask_qty,
                volume=quote.volume,
                open_interest=quote.open_interest,
            )

    # DataSource protocol — for tick-by-tick mode
    def stream(self) -> Iterator[Bar | Tick]:
        # We can't yield from an async generator into a sync iterator without a
        # running loop. The engine's run loop in live mode should be async — see
        # `LiveEngine.run_async`.
        raise RuntimeError("LiveDataSource is async-only; use LiveEngine.run_async()")

    def latest_price(self, instrument: Instrument) -> float | None:
        return self._last_price.get(instrument.id)
