"""Download & cache NSE EQ bhavcopy CSVs over a date range.

One request per trading day returns OHLCV + turnover + delivery% for every
listed equity — deep, free history that sidesteps the per-symbol AngelOne
rate limits. CSVs are cached to ``data/bhavcopy_eq/YYYY-MM-DD.csv`` so
re-parsing never re-downloads. Weekends and holidays (HTTP 404) are skipped.

Run:
    PYTHONPATH=. .venv/bin/python scripts/ingest_bhavcopy_history.py \
        --start 2025-08-13 --end 2026-08-12
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from qbacktest.data.sources.bhavcopy import download_bhavcopy_eq

CACHE_DIR = Path("data/bhavcopy_eq")


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--pace", type=float, default=0.8, help="seconds between requests")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    have = miss = fetched = err = 0
    for d in daterange(start, end):
        if d.weekday() >= 5:            # weekend — NSE closed
            continue
        dest = CACHE_DIR / f"{d.isoformat()}.csv"
        if dest.exists() and dest.stat().st_size > 1000:
            have += 1
            continue
        try:
            download_bhavcopy_eq(d, dest=dest)
            fetched += 1
        except httpx.HTTPStatusError as e:
            # 404 = trading holiday (no bhavcopy that day) — expected, skip.
            if e.response.status_code == 404:
                miss += 1
            else:
                err += 1
                print(f"  {d}: HTTP {e.response.status_code}")
        except Exception as e:
            err += 1
            print(f"  {d}: {type(e).__name__} {str(e)[:60]}")
        time.sleep(args.pace)

    print(f"bhavcopy {start}..{end}: {fetched} fetched | {have} cached | "
          f"{miss} holidays | {err} errors | cache={CACHE_DIR}")


if __name__ == "__main__":
    main()
