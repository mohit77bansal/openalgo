"""Download & cache NSE F&O bhavcopy over a date range.

The FO bhavcopy carries daily OHLC + settlement + **open interest** for every
option and future contract that traded that day — deep multi-year derivatives
data (unlike 1m, which the broker only serves for still-listed contracts). This
is the honest path to backtesting option structures / OI signals on 2-3+ years,
at daily resolution.

CSVs are cached to ``data/bhavcopy_fo/YYYY-MM-DD.csv`` so re-parsing never
re-downloads; weekends and holidays (HTTP 404) are skipped.

Run:
    PYTHONPATH=. .venv/bin/python scripts/ingest_fo_bhavcopy_history.py \
        --start 2022-08-15 --end 2026-08-12
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from qbacktest.data.sources.bhavcopy import download_bhavcopy_fo

CACHE_DIR = Path("data/bhavcopy_fo")


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--pace", type=float, default=0.8)
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    have = miss = fetched = err = 0
    for d in daterange(start, end):
        if d.weekday() >= 5:
            continue
        dest = CACHE_DIR / f"{d.isoformat()}.csv"
        if dest.exists() and dest.stat().st_size > 5000:
            have += 1
            continue
        try:
            download_bhavcopy_fo(d, dest=dest)
            fetched += 1
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                miss += 1                       # trading holiday
            else:
                err += 1
                print(f"  {d}: HTTP {e.response.status_code}")
        except Exception as e:
            err += 1
            print(f"  {d}: {type(e).__name__} {str(e)[:60]}")
        time.sleep(args.pace)

    print(f"FO bhavcopy {start}..{end}: {fetched} fetched | {have} cached | "
          f"{miss} holidays | {err} errors | cache={CACHE_DIR}")


if __name__ == "__main__":
    main()
