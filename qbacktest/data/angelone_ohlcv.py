"""AngelOne 1-min OHLCV ingestor.

Fetches historical candles for a set of instruments via the AngelOne SmartAPI
historical endpoint and persists them to the DataStore's existing partitioned
Parquet layout.

Rate-limit notes:
  - AngelOne historical API: ~3 req/sec is the safe practical limit (the
    documented per-second cap is higher but bans happen quickly under burst).
  - Per-request span: SmartAPI typically returns up to ~500 candles per call,
    so a 5-day fetch at 1-min interval (≈1,875 bars) needs one call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from qbacktest.brokers.angelone import AngelOneAdapter
from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar, Instrument, InstrumentType
from qbacktest.data.angelone_chain import (
    _find_next_expiry,
    _find_spot_index,
    _load_symbol_master,
    _strikes_for_expiry,
)
from qbacktest.data.io import bars_to_parquet
from qbacktest.data.store import DataStore

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    instrument_id: str
    timeframe: str
    days_covered: int
    bars_written: int
    files_written: int
    error: Optional[str] = None


def _group_bars_by_day(bars: list[Bar]) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = {}
    for b in bars:
        d = b.ts.astimezone(IST).date()
        out.setdefault(d, []).append(b)
    return out


def _write_day(store: DataStore, inst: Instrument, d: date, day_bars: list[Bar], timeframe: str) -> Path:
    """Write one day of bars using the canonical Parquet schema (data/io.py)."""
    p = store.bar_path(inst, d, timeframe=timeframe)
    bars_to_parquet(day_bars, p, compression="zstd")
    return p


async def ingest_instrument(
    adapter: AngelOneAdapter,
    instrument: Instrument,
    start: date,
    end: date,
    timeframe: str = "1m",
    store: Optional[DataStore] = None,
    overwrite: bool = False,
) -> IngestResult:
    """Fetch + persist OHLCV for a single instrument over [start, end]."""
    from qbacktest.gateway.settings import load_settings
    store = store or DataStore(root=load_settings().app_data_dir)

    # Skip whole-range if all days already present and overwrite=False.
    if not overwrite:
        missing = [
            d for d in _iter_dates(start, end)
            if not store.bar_path(instrument, d, timeframe=timeframe).exists()
        ]
        if not missing:
            return IngestResult(
                instrument_id=instrument.id, timeframe=timeframe,
                days_covered=0, bars_written=0, files_written=0,
            )

    # AngelOne caps minute-interval historical calls at ~30 days per request.
    # Chunk the window so a 1-year ingest at 1-min interval actually returns
    # a year of data, not just the most recent ~30 days.
    chunk_days = 28 if timeframe in ("1m", "3m", "5m") else 365
    bars: list[Bar] = []
    last_err: Optional[str] = None
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        for attempt in range(4):
            try:
                chunk = await adapter.historical_candles(
                    instrument, cursor, chunk_end, timeframe=timeframe,
                )
                bars.extend(chunk)
                last_err = None
                break
            except Exception as e:
                msg = str(e)
                last_err = msg
                # Retry on rate limits AND transient network errors. DNS hiccups
                # (nodename nor servname / getaddrinfo) kill long-running ingests
                # otherwise.
                retryable = (
                    "access rate" in msg.lower()
                    or "rate limit" in msg.lower()
                    or "nodename" in msg.lower()
                    or "getaddrinfo" in msg.lower()
                    or "connection" in msg.lower()
                    or "timeout" in msg.lower()
                    or "errno 8" in msg.lower()
                )
                if retryable:
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                break
        cursor = chunk_end + timedelta(days=1)
        if last_err is not None:
            break
        # gentle inter-chunk pause to stay under per-second rate limit
        await asyncio.sleep(0.4)
    if last_err is not None and not bars:
        return IngestResult(
            instrument_id=instrument.id, timeframe=timeframe,
            days_covered=0, bars_written=0, files_written=0, error=last_err,
        )

    if not bars:
        return IngestResult(
            instrument_id=instrument.id, timeframe=timeframe,
            days_covered=0, bars_written=0, files_written=0,
        )

    by_day = _group_bars_by_day(bars)
    files_written = 0
    bars_written = 0
    for d, day_bars in by_day.items():
        p = store.bar_path(instrument, d, timeframe=timeframe)
        if p.exists() and not overwrite:
            continue
        _write_day(store, instrument, d, day_bars, timeframe)
        files_written += 1
        bars_written += len(day_bars)

    return IngestResult(
        instrument_id=instrument.id, timeframe=timeframe,
        days_covered=len(by_day), bars_written=bars_written, files_written=files_written,
    )


def _iter_dates(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:  # weekdays only — NSE closed Sat/Sun
            yield d
        d += timedelta(days=1)


async def ingest_many(
    adapter: AngelOneAdapter,
    instruments: list[Instrument],
    start: date,
    end: date,
    timeframe: str = "1m",
    pace_per_second: float = 1.2,
    store: Optional[DataStore] = None,
    overwrite: bool = False,
) -> list[IngestResult]:
    """Rate-limited bulk ingest. Yields one IngestResult per instrument."""
    interval = 1.0 / max(pace_per_second, 0.1)
    out: list[IngestResult] = []
    for i, inst in enumerate(instruments):
        if i > 0:
            await asyncio.sleep(interval)
        res = await ingest_instrument(
            adapter, inst, start, end, timeframe=timeframe,
            store=store, overwrite=overwrite,
        )
        if res.error:
            log.warning("Ingest failed for %s: %s", inst.id, res.error)
        else:
            log.info(
                "Ingest %s: %d bars across %d days (%d files)",
                inst.id, res.bars_written, res.days_covered, res.files_written,
            )
        out.append(res)
    return out


async def discover_chain_instruments(
    adapter: AngelOneAdapter,
    underlying: str,
    expiry_count: int = 2,
    strikes_each_side: int = 10,
) -> tuple[list[Instrument], list[Instrument]]:
    """Walk the symbol master to pick ATM±N strikes × CE/PE for next N expiries.

    Returns (spot_indices, option_instruments). The spot index is included so
    the caller can ingest the underlying bars in the same pass.
    """
    master = await _load_symbol_master(adapter)
    spot = _find_spot_index(master, underlying)
    if spot is None:
        log.warning("No spot index for %s", underlying)
        return [], []

    today = datetime.now(IST).date()

    # Approximate ATM from the last historical daily close (one cheap call).
    atm_ref: float
    try:
        bars = await adapter.historical_candles(spot, today - timedelta(days=7), today, timeframe="1d")
        atm_ref = float(bars[-1].close) if bars else 0.0
    except Exception:
        atm_ref = 0.0

    # Find next N expiries
    expiries: list[date] = []
    cursor = today
    for _ in range(expiry_count):
        nxt = _find_next_expiry(
            [i for i in master if i.expiry is None or i.expiry >= cursor],
            underlying,
            today=cursor,
        )
        if nxt is None or nxt in expiries:
            break
        expiries.append(nxt)
        cursor = nxt + timedelta(days=1)

    selected: list[Instrument] = []
    for exp in expiries:
        all_strikes = _strikes_for_expiry(master, underlying, exp)
        if not all_strikes:
            continue
        if atm_ref <= 0:
            atm_ref = all_strikes[len(all_strikes) // 2]
        atm = min(all_strikes, key=lambda k: abs(k - atm_ref))
        idx = all_strikes.index(atm)
        lo = max(0, idx - strikes_each_side)
        hi = min(len(all_strikes), idx + strikes_each_side + 1)
        targeted = set(all_strikes[lo:hi])

        for inst in master:
            if (
                inst.instrument_type in (InstrumentType.CE, InstrumentType.PE)
                and inst.underlying
                and inst.underlying.upper() == underlying.upper()
                and inst.expiry == exp
                and inst.strike is not None
                and float(inst.strike) in targeted
            ):
                selected.append(inst)

    return [spot], selected
