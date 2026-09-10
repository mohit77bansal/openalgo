"""Option 1m data coverage report — track accumulation toward 'enough'.

Prints per-day contract/bar counts and flags missing trading days, so we can
see the dataset growing and catch collection gaps early (a missing weekday is
a red flag the collector or its token failed that session).

Run:
    PYTHONPATH=. .venv/bin/python scripts/option_data_coverage.py
"""

from __future__ import annotations

import duckdb

DB = "db/historify.duckdb"
TARGET_DAYS = 30  # rough bar for validating a regime signal beyond fluke


def main() -> None:
    conn = duckdb.connect(DB, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT CAST(to_timestamp(timestamp) AT TIME ZONE 'Asia/Kolkata' AS DATE) d,
                   COUNT(DISTINCT symbol) contracts, COUNT(*) bars
            FROM market_data
            WHERE interval='1m' AND (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE')
            GROUP BY d ORDER BY d
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("no option data yet")
        return

    print(f"{'day':<12}{'weekday':<10}{'contracts':>10}{'bars':>9}")
    print("-" * 41)
    import datetime as _dt

    days = [r[0] for r in rows]
    for d, c, b in rows:
        wd = d.strftime("%a")
        flag = "  ⚠ thin" if b < 15000 else ""
        print(f"{str(d):<12}{wd:<10}{c:>10}{b:>9}{flag}")

    # Flag missing weekdays inside the covered span (holidays excluded manually).
    span = (days[-1] - days[0]).days
    missing = []
    for i in range(span + 1):
        day = days[0] + _dt.timedelta(days=i)
        if day.weekday() < 5 and day not in days:
            missing.append(day)
    print("-" * 41)
    print(f"trading days collected: {len(days)}  (target ~{TARGET_DAYS} to validate a signal)")
    if missing:
        print(f"missing weekdays in span (holiday or collection gap): "
              f"{', '.join(str(m) for m in missing)}")
    else:
        print("no missing weekdays in covered span ✓")


if __name__ == "__main__":
    main()
