"""Historical DataSource — feeds bars from the Parquet store into the engine.

Implements the DataSource protocol expected by `BacktestEngine`. Yields bars in
strict timestamp order across multiple subscribed instruments.
"""

from __future__ import annotations

from datetime import date, datetime
from heapq import heappush, heappop
from typing import Iterable, Iterator, Optional

from qbacktest.core.calendar import DEFAULT_CALENDAR, TradingCalendar
from qbacktest.core.types import Bar, Instrument, Tick
from qbacktest.data.io import parquet_to_bars
from qbacktest.data.store import DataStore


class HistoricalSource:
    """Replays Parquet bars in time order for a list of subscribed instruments.

    Usage:
        src = HistoricalSource(store)
        src.subscribe(nifty_index, start=date(2024,1,1), end=date(2024,12,31), timeframe="1d")
        src.subscribe(nifty_dec_25000_ce, start=..., end=..., timeframe="1d")
        engine = BacktestEngine(strategy, data=src)
        result = engine.run()
    """

    def __init__(
        self,
        store: DataStore,
        *,
        calendar: Optional[TradingCalendar] = None,
    ) -> None:
        self.store = store
        self.calendar = calendar or DEFAULT_CALENDAR
        self._subscriptions: list[tuple[Instrument, date, date, str]] = []
        self._latest_price: dict[str, float] = {}

    def subscribe(
        self,
        instrument: Instrument,
        *,
        start: date,
        end: date,
        timeframe: str = "1d",
    ) -> None:
        self._subscriptions.append((instrument, start, end, timeframe))

    def _load_bars(self, instrument: Instrument, start: date, end: date, timeframe: str) -> Iterator[Bar]:
        for d in self.calendar.trading_days_between(start, end):
            path = self.store.bar_path(instrument, d, timeframe)
            if not path.exists():
                continue
            for b in parquet_to_bars(path):
                yield b

    def stream(self) -> Iterator[Bar | Tick]:
        # Min-heap of (ts, monotonic counter, source iterator, current bar)
        heap: list = []
        sources = []
        for i, (inst, start, end, tf) in enumerate(self._subscriptions):
            it = self._load_bars(inst, start, end, tf)
            try:
                first = next(it)
                heappush(heap, (first.ts, i, first))
                sources.append(it)
            except StopIteration:
                sources.append(iter([]))

        while heap:
            ts, src_i, bar = heappop(heap)
            self._latest_price[bar.instrument.id] = bar.close
            yield bar
            try:
                nxt = next(sources[src_i])
                heappush(heap, (nxt.ts, src_i, nxt))
            except StopIteration:
                continue

    def latest_price(self, instrument: Instrument) -> Optional[float]:
        return self._latest_price.get(instrument.id)
