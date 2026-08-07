"""Filesystem layout for the Parquet data store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from qbacktest.core.types import Instrument, InstrumentType


@dataclass(frozen=True)
class DataStore:
    """Resolves canonical paths for historical data.

    Layout is partitioned for efficient DuckDB pruning. Each partition is a
    self-contained Parquet file so single-day queries don't pay scan cost on the
    rest of the year.
    """

    root: Path

    def __post_init__(self) -> None:
        # Ensure paths exist
        for sub in ("parquet/bars/eq", "parquet/bars/fut", "parquet/bars/opt", "parquet/bars/index", "raw/bhavcopy", "duckdb"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    @property
    def parquet_root(self) -> Path:
        return self.root / "parquet"

    @property
    def raw_root(self) -> Path:
        return self.root / "raw"

    @property
    def duckdb_path(self) -> Path:
        return self.root / "duckdb" / "catalog.duckdb"

    def bar_path(self, instrument: Instrument, d: date, timeframe: str = "1d") -> Path:
        """Resolve canonical Parquet path for one (instrument, date, timeframe).

        We partition by year-month to keep the directory tree manageable.
        """
        ym = f"{d.year:04d}/{d.month:02d}"
        if instrument.instrument_type == InstrumentType.EQ:
            return self.parquet_root / "bars/eq" / instrument.exchange.value / instrument.symbol / ym / f"{d.isoformat()}-{timeframe}.parquet"
        if instrument.instrument_type == InstrumentType.INDEX:
            return self.parquet_root / "bars/index" / instrument.exchange.value / instrument.symbol / ym / f"{d.isoformat()}-{timeframe}.parquet"
        if instrument.instrument_type == InstrumentType.FUT:
            assert instrument.expiry
            exp = instrument.expiry.isoformat()
            return self.parquet_root / "bars/fut" / (instrument.underlying or instrument.symbol) / exp / ym / f"{d.isoformat()}-{timeframe}.parquet"
        # Option
        assert instrument.expiry and instrument.strike is not None
        exp = instrument.expiry.isoformat()
        strike_part = f"{int(instrument.strike)}" if float(instrument.strike).is_integer() else f"{instrument.strike:g}"
        return (
            self.parquet_root / "bars/opt"
            / (instrument.underlying or instrument.symbol)
            / exp
            / strike_part
            / instrument.instrument_type.value
            / ym
            / f"{d.isoformat()}-{timeframe}.parquet"
        )

    def bhavcopy_path(self, d: date, segment: str = "EQ") -> Path:
        """EQ or FO bhavcopy CSV (raw) on a given date."""
        return self.raw_root / "bhavcopy" / segment.upper() / f"{d.isoformat()}.csv"

    def list_dates_for(self, instrument: Instrument, timeframe: str = "1d") -> list[date]:
        """Return all dates we have parquet files for the instrument+timeframe."""
        # Walk the partitions for this instrument
        if instrument.instrument_type == InstrumentType.EQ:
            base = self.parquet_root / "bars/eq" / instrument.exchange.value / instrument.symbol
        elif instrument.instrument_type == InstrumentType.INDEX:
            base = self.parquet_root / "bars/index" / instrument.exchange.value / instrument.symbol
        elif instrument.instrument_type == InstrumentType.FUT and instrument.expiry:
            base = self.parquet_root / "bars/fut" / (instrument.underlying or instrument.symbol) / instrument.expiry.isoformat()
        elif instrument.is_option and instrument.expiry and instrument.strike is not None:
            strike_part = f"{int(instrument.strike)}" if float(instrument.strike).is_integer() else f"{instrument.strike:g}"
            base = (
                self.parquet_root / "bars/opt"
                / (instrument.underlying or instrument.symbol)
                / instrument.expiry.isoformat()
                / strike_part
                / instrument.instrument_type.value
            )
        else:
            return []
        if not base.exists():
            return []
        out: list[date] = []
        for p in base.rglob(f"*-{timeframe}.parquet"):
            try:
                stem_date = p.stem.split("-")[0:3]
                out.append(date.fromisoformat("-".join(stem_date)))
            except ValueError:
                continue
        out.sort()
        return out
