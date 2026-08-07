"""Data availability checker.

Surfaces "what data do you have for X over Y?" before a backtest runs, so the
user isn't surprised by hollow ranges (especially for free-only options data
which has gaps).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from qbacktest.data.store import DataStore
from qbacktest.core.types import Instrument


@dataclass
class AvailabilityReport:
    instrument_id: str
    timeframe: str
    earliest: date | None
    latest: date | None
    n_days: int
    missing_in_range: list[date]


class DataAvailability:
    """Inspects the local Parquet store; reports per-instrument coverage."""

    def __init__(self, store: DataStore) -> None:
        self.store = store

    def report(
        self,
        instrument: Instrument,
        timeframe: str = "1d",
        *,
        expected_start: date | None = None,
        expected_end: date | None = None,
        calendar=None,
    ) -> AvailabilityReport:
        dates = self.store.list_dates_for(instrument, timeframe)
        earliest = dates[0] if dates else None
        latest = dates[-1] if dates else None
        n_days = len(dates)

        missing: list[date] = []
        if expected_start and expected_end and calendar is not None:
            expected = calendar.trading_days_between(expected_start, expected_end)
            have = set(dates)
            missing = [d for d in expected if d not in have]

        return AvailabilityReport(
            instrument_id=instrument.id,
            timeframe=timeframe,
            earliest=earliest,
            latest=latest,
            n_days=n_days,
            missing_in_range=missing,
        )

    def summary_for(
        self,
        instruments: list[Instrument],
        timeframe: str = "1d",
    ) -> list[AvailabilityReport]:
        return [self.report(i, timeframe) for i in instruments]
