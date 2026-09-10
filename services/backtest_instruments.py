"""Backtest instruments — interval mapping, instrument construction, and data sources.

Extracted from backtest_service.py.  Contains the plumbing that turns
OpenAlgo symbols + exchanges into qbacktest ``Instrument`` / ``Bar`` objects
and wraps raw data (synthetic or DuckDB) into a ``DataSource`` the engine
can consume.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any


# --------------------------------------------------------------------------- #
# Interval mapping (OpenAlgo -> qbacktest timeframe label)
# --------------------------------------------------------------------------- #
_INTERVAL_TF: dict[str, str] = {
    "D": "1d", "1d": "1d",
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
}


# --------------------------------------------------------------------------- #
# Futures expiry parsing
# --------------------------------------------------------------------------- #
_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_FUT_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})FUT$")


def _parse_fut_expiry(symbol: str) -> date | None:
    """Parse an F&O expiry from an OpenAlgo future symbol, e.g. NIFTY25AUG26FUT."""
    m = _FUT_RE.search((symbol or "").upper())
    if not m:
        return None
    dd, mon, yy = m.groups()
    return date(2000 + int(yy), _MONTHS[mon], int(dd))


# --------------------------------------------------------------------------- #
# Lot-size lookup (master contract DB -> hardcoded fallback)
# --------------------------------------------------------------------------- #
def _lookup_lot_size(symbol: str, exchange: str) -> int:
    """Look up the actual lot size from the OpenAlgo master contract DB."""
    try:
        # Lazy import — avoids import-time DB hit.
        from database.symbol import db_session, SymToken

        row = db_session.query(SymToken).filter(
            SymToken.symbol == symbol, SymToken.exchange == exchange
        ).first()
        if row and row.lotsize and row.lotsize > 0:
            return int(row.lotsize)
    except Exception:
        pass
    # Fallback: known lot sizes for major indices (as of Aug 2026)
    for prefix, lot in [("BANKNIFTY", 30), ("FINNIFTY", 60), ("MIDCPNIFTY", 120), ("NIFTY", 65)]:
        if symbol.upper().startswith(prefix):
            return lot
    return 1


# --------------------------------------------------------------------------- #
# Instrument construction
# --------------------------------------------------------------------------- #
def _instrument_for(symbol: str, exchange: str) -> Any:
    """Build a qbacktest ``Instrument`` from an OpenAlgo symbol + exchange."""
    # Lazy imports — keep qbacktest out of module-level scope.
    from qbacktest.core.types import Exchange, Instrument, InstrumentType

    ex = (exchange or "NSE").upper()
    if ex in ("NSE_INDEX", "BSE_INDEX", "INDEX"):
        qex = Exchange.BSE if ex == "BSE_INDEX" else Exchange.NSE
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.INDEX, lot_size=1)
    if ex in ("NFO", "BFO"):
        qex = Exchange.BFO if ex == "BFO" else Exchange.NFO
        lot = _lookup_lot_size(symbol, ex)
        return Instrument(
            symbol=symbol, exchange=qex, instrument_type=InstrumentType.FUT,
            lot_size=lot, expiry=_parse_fut_expiry(symbol),
        )
    qex = getattr(Exchange, ex, Exchange.NSE)
    return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.EQ, lot_size=1)


# --------------------------------------------------------------------------- #
# _StaticSource — lightweight DataSource backed by an in-memory bar list
# --------------------------------------------------------------------------- #
class _StaticSource:
    """A qbacktest DataSource backed by an in-memory list of Bars."""

    def __init__(self, bars: list[Any]):
        self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
        self._latest: dict[str, float] = {}

    def stream(self):
        for b in self.bars:
            self._latest[b.instrument.id] = b.close
            yield b

    def latest_price(self, instrument: Any) -> float | None:
        return self._latest.get(instrument.id)


# --------------------------------------------------------------------------- #
# "demo" — deterministic synthetic bars
# --------------------------------------------------------------------------- #
def _build_synthetic_source(symbol: str, exchange: str, n_bars: int) -> _StaticSource:
    """Generate ``n_bars`` of deterministic synthetic daily bars."""
    # Lazy imports.
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar

    inst = _instrument_for(symbol, exchange)
    base = 25_000.0
    bars: list[Any] = []
    d, made, i = date(2024, 1, 1), 0, 0
    while made < n_bars:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:
            continue
        px = base * (1.0 + 0.0008 * i + 0.02 * math.sin(i / 6.0))
        bars.append(Bar(
            instrument=inst, ts=datetime.combine(d, time(15, 30), tzinfo=IST),
            timeframe="1d", open=px, high=px * 1.004, low=px * 0.996, close=px * 1.001, volume=1000,
        ))
        made += 1
        i += 1
    return _StaticSource(bars)


# --------------------------------------------------------------------------- #
# "db" — real OHLCV from the Historify DuckDB store
# --------------------------------------------------------------------------- #
def _build_db_source(
    symbol: str, exchange: str, interval: str,
    start: str | None, end: str | None,
) -> tuple[_StaticSource, int]:
    """Build a ``_StaticSource`` from the Historify DuckDB store.

    Returns ``(source, n_bars)`` or raises ``ValueError('no data ...')``.
    """
    # Lazy imports.
    from database.historify_db import get_connection, get_ohlcv
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar
    import pandas as pd

    start_ts = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_ts = int(datetime.fromisoformat(end).timestamp()) if end else None

    # Try the standard get_ohlcv first (handles storage intervals + aggregation)
    df = get_ohlcv(symbol, exchange, interval, start_timestamp=start_ts, end_timestamp=end_ts)

    # If empty, query DuckDB directly — data may be stored at this interval
    # but not in Historify's STORAGE_INTERVALS list (e.g. 5m, 15m, 1h ingested
    # directly from the broker).
    if df is None or len(df) == 0:
        try:
            with get_connection() as con:
                sql = (
                    "SELECT timestamp, open, high, low, close, volume, oi "
                    "FROM market_data "
                    "WHERE symbol=? AND exchange=? AND interval=? "
                )
                params: list[Any] = [symbol, exchange, interval]
                if start_ts:
                    sql += "AND timestamp >= ? "
                    params.append(start_ts)
                if end_ts:
                    sql += "AND timestamp <= ? "
                    params.append(end_ts)
                sql += "ORDER BY timestamp ASC"
                df = con.execute(sql, params).fetchdf()
        except Exception:
            df = pd.DataFrame()

    if df is None or len(df) == 0:
        raise ValueError(
            f"no {interval} data for {symbol}/{exchange} in the Historify store — "
            f"ingest it from AngelOne first"
        )

    inst = _instrument_for(symbol, exchange)
    tf = _INTERVAL_TF.get(interval, "1d")
    bars: list[Any] = []
    for row in df.itertuples(index=False):
        ts = datetime.fromtimestamp(int(row.timestamp), tz=timezone.utc).astimezone(IST)
        bars.append(Bar(
            instrument=inst, ts=ts, timeframe=tf,
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(getattr(row, "volume", 0) or 0),
        ))
    return _StaticSource(bars), len(bars)
