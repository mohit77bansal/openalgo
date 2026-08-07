"""Historical lot-size table for NSE F&O instruments.

NSE revises lot sizes periodically (NIFTY: 75 → 50 → 25 → 75; BANKNIFTY similar).
A backtest must use the lot size *active on the trade date*, not today's lot size.

Data is keyed by (underlying, effective_from_date). Lookup returns the most recent
override at or before the query date.

Source: NSE F&O instrument-master files. Hardcoded historical points; live updates
via `qbacktest.data.sources.nse.refresh_lot_sizes()`.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LotSizeEntry:
    underlying: str
    effective_from: date
    lot_size: int


# Historical lot-size revisions. Sorted by effective_from per underlying.
# This is illustrative; the canonical source is NSE's instrument master.
_HISTORY: dict[str, list[LotSizeEntry]] = {
    "NIFTY": [
        LotSizeEntry("NIFTY", date(2010, 1, 1), 50),
        LotSizeEntry("NIFTY", date(2015, 10, 30), 75),
        LotSizeEntry("NIFTY", date(2021, 10, 29), 50),
        LotSizeEntry("NIFTY", date(2024, 9, 27), 25),
        LotSizeEntry("NIFTY", date(2024, 11, 26), 75),
    ],
    "BANKNIFTY": [
        LotSizeEntry("BANKNIFTY", date(2010, 1, 1), 25),
        LotSizeEntry("BANKNIFTY", date(2015, 10, 30), 40),
        LotSizeEntry("BANKNIFTY", date(2016, 7, 1), 30),
        LotSizeEntry("BANKNIFTY", date(2017, 7, 1), 40),
        LotSizeEntry("BANKNIFTY", date(2019, 4, 1), 20),
        LotSizeEntry("BANKNIFTY", date(2020, 4, 1), 25),
        LotSizeEntry("BANKNIFTY", date(2023, 7, 28), 15),
        LotSizeEntry("BANKNIFTY", date(2024, 4, 26), 15),
        LotSizeEntry("BANKNIFTY", date(2024, 11, 26), 30),
    ],
    "FINNIFTY": [
        LotSizeEntry("FINNIFTY", date(2021, 1, 11), 40),
        LotSizeEntry("FINNIFTY", date(2024, 4, 26), 25),
        LotSizeEntry("FINNIFTY", date(2024, 11, 26), 65),
    ],
    "MIDCPNIFTY": [
        LotSizeEntry("MIDCPNIFTY", date(2024, 7, 8), 75),
        LotSizeEntry("MIDCPNIFTY", date(2024, 11, 26), 120),
    ],
    "SENSEX": [
        LotSizeEntry("SENSEX", date(2024, 1, 1), 10),
        LotSizeEntry("SENSEX", date(2024, 11, 26), 20),
    ],
    "BANKEX": [
        LotSizeEntry("BANKEX", date(2024, 1, 1), 15),
        LotSizeEntry("BANKEX", date(2024, 11, 26), 30),
    ],
    # Stock futures — small selection. Full table loaded from instrument master.
    "RELIANCE": [
        LotSizeEntry("RELIANCE", date(2020, 1, 1), 505),
        LotSizeEntry("RELIANCE", date(2023, 7, 28), 250),
    ],
    "TCS": [
        LotSizeEntry("TCS", date(2020, 1, 1), 300),
        LotSizeEntry("TCS", date(2023, 7, 28), 175),
    ],
    "HDFCBANK": [
        LotSizeEntry("HDFCBANK", date(2020, 1, 1), 550),
        LotSizeEntry("HDFCBANK", date(2023, 7, 28), 550),
    ],
    "INFY": [
        LotSizeEntry("INFY", date(2020, 1, 1), 600),
        LotSizeEntry("INFY", date(2023, 7, 28), 400),
    ],
}


def lot_size_on(underlying: str, on: date) -> int:
    """Return the lot size for `underlying` active on date `on`.

    Raises KeyError if the underlying has no recorded history. Strategies should
    call this every time they construct an order — never hardcode.
    """
    entries = _HISTORY.get(underlying.upper())
    if entries is None:
        raise KeyError(f"No lot-size history for {underlying!r}. Refresh from NSE.")
    eff_dates = [e.effective_from for e in entries]
    idx = bisect_right(eff_dates, on) - 1
    if idx < 0:
        raise KeyError(f"No lot-size for {underlying!r} on or before {on}")
    return entries[idx].lot_size


def latest_lot_size(underlying: str) -> int:
    """Convenience: today's lot size."""
    entries = _HISTORY.get(underlying.upper())
    if not entries:
        raise KeyError(f"No lot-size history for {underlying!r}")
    return entries[-1].lot_size


def known_underlyings() -> list[str]:
    return sorted(_HISTORY.keys())


def register_lot_size(entry: LotSizeEntry) -> None:
    """Inject a new lot-size revision (e.g. from instrument-master refresh)."""
    bucket = _HISTORY.setdefault(entry.underlying.upper(), [])
    bucket.append(entry)
    bucket.sort(key=lambda e: e.effective_from)
