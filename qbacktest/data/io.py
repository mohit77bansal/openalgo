"""Bar (de)serialization to Parquet."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from qbacktest.core.types import Bar, Exchange, Instrument, InstrumentType


_BAR_SCHEMA = pa.schema([
    pa.field("ts", pa.timestamp("ms", tz="Asia/Kolkata")),
    pa.field("symbol", pa.string()),
    pa.field("exchange", pa.string()),
    pa.field("instrument_type", pa.string()),
    pa.field("expiry", pa.date32()),
    pa.field("strike", pa.float64()),
    pa.field("underlying", pa.string()),
    pa.field("timeframe", pa.string()),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64()),
    pa.field("volume", pa.int64()),
    pa.field("open_interest", pa.int64()),
    pa.field("lot_size", pa.int32()),
])


def bars_to_parquet(bars: Iterable[Bar], path: Path, *, compression: str = "zstd") -> int:
    """Write bars to a Parquet file. Creates parent directories. Returns row count."""
    rows = []
    for b in bars:
        rows.append({
            "ts": b.ts,
            "symbol": b.instrument.symbol,
            "exchange": b.instrument.exchange.value,
            "instrument_type": b.instrument.instrument_type.value,
            "expiry": b.instrument.expiry,
            "strike": b.instrument.strike,
            "underlying": b.instrument.underlying,
            "timeframe": b.timeframe,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "open_interest": b.open_interest,
            "lot_size": b.instrument.lot_size,
        })
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=_BAR_SCHEMA)
    pq.write_table(table, path, compression=compression)
    return len(rows)


def parquet_to_bars(path: Path) -> list[Bar]:
    """Read bars from a single Parquet file. Reconstructs Instrument."""
    if not path.exists():
        return []
    table = pq.read_table(path)
    out: list[Bar] = []
    for row in table.to_pylist():
        inst = Instrument(
            symbol=row["symbol"],
            exchange=Exchange(row["exchange"]),
            instrument_type=InstrumentType(row["instrument_type"]),
            lot_size=int(row.get("lot_size") or 1),
            expiry=row["expiry"],
            strike=row["strike"],
            underlying=row["underlying"],
        )
        out.append(Bar(
            instrument=inst,
            ts=row["ts"] if isinstance(row["ts"], datetime) else datetime.fromisoformat(str(row["ts"])),
            timeframe=row["timeframe"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row.get("volume") or 0),
            open_interest=row.get("open_interest"),
        ))
    return out
