"""Resilient daily option 1m backfill — the accumulation safety net.

The live in-app collector only works while the Flask app is running AND its
AngelOne token is fresh. Angel tokens die at the ~03:00 IST daily rollover but
auto-auth runs only at app boot, so a multi-day-running app silently collects
nothing after the first night. This script closes that hole:

  * **Self-contained auth** — re-authenticates from .env every run, so it works
    from cron/launchd with no dependency on the app being up.
  * **Date-range fetch** — pulls a trailing window (default 5 days) of 1m bars
    for every near-ATM tracked contract, recovering whole missed sessions and
    filling gaps left by intraday rate-limiting.
  * **Idempotent** — upserts into the same DuckDB the backtester reads, so
    re-running never duplicates.

Run manually:
    PYTHONPATH=. .venv/bin/python scripts/daily_option_backfill.py --days 5

Schedule (after 15:45 IST on trading days) to guarantee daily accumulation.
"""

from __future__ import annotations

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import pyotp

from broker.angel.api.auth_api import authenticate_broker
from broker.angel.api.data import BrokerData
from database.auth_db import get_auth_token, upsert_auth
from database.historify_db import init_database, upsert_market_data
from services.options_data_collector import OptionsDataCollector
from utils.logging import get_logger

IST = timezone(timedelta(hours=5, minutes=30))
logger = get_logger(__name__)


def ensure_fresh_auth() -> str | None:
    """Re-authenticate from .env and persist under both key names. Falls back to
    the stored token if TOTP auth fails (e.g. transient network)."""
    user = os.getenv("LOGIN_USERNAME", "mohit")
    cc = os.getenv("ANGELONE_CLIENT_CODE")
    pin = os.getenv("ANGELONE_PIN")
    sec = os.getenv("ANGELONE_TOTP_SECRET")
    if not (cc and pin and sec):
        logger.error("Missing AngelOne .env credentials; cannot backfill")
        return get_auth_token(user, bypass_cache=True)
    try:
        tok, feed, err = authenticate_broker(cc, pin, pyotp.TOTP(sec).now())
        if tok:
            upsert_auth(os.getenv("BROKER_API_KEY", ""), tok, "angel", feed_token=feed)
            upsert_auth(user, tok, "angel", feed_token=feed, revoke=False)
            logger.info("Backfill: refreshed AngelOne token")
            return tok
        logger.warning(f"Backfill auth failed ({err}); using stored token")
    except Exception:
        logger.exception("Backfill auth error; using stored token")
    return get_auth_token(user, bypass_cache=True)


def resolve_symbols(broker: BrokerData) -> list[dict]:
    """Near-ATM tracked contracts via the collector's own resolution logic."""
    coll = OptionsDataCollector()
    coll._refresh_symbols(broker)
    return list(coll._tracked_symbols)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="trailing days to fetch")
    ap.add_argument("--pace", type=float, default=0.5, help="seconds between API calls")
    args = ap.parse_args()

    init_database()
    tok = ensure_fresh_auth()
    if not tok:
        logger.error("No usable auth token; aborting backfill")
        return
    broker = BrokerData(tok)

    symbols = resolve_symbols(broker)
    if not symbols:
        logger.error("No tracked symbols resolved; aborting")
        return

    end = datetime.now(IST).date()
    start = end - timedelta(days=args.days)
    s0, s1 = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    stored = errors = empty = 0
    for entry in symbols:
        sym, exch = entry["symbol"], entry["exchange"]
        try:
            df = broker.get_history(sym, exch, "1m", s0, s1)
            if df is not None and not df.empty:
                upsert_market_data(df, sym, exch, "1m")
                stored += 1
            else:
                empty += 1
        except Exception:
            logger.debug("Backfill error for %s/%s", sym, exch, exc_info=True)
            errors += 1
        time.sleep(args.pace)

    logger.info(
        "Backfill %s..%s complete: %d symbols, %d stored, %d empty, %d errors",
        s0, s1, len(symbols), stored, empty, errors,
    )
    print(f"backfill {s0}..{s1}: {len(symbols)} symbols | {stored} stored | "
          f"{empty} empty | {errors} errors")


if __name__ == "__main__":
    main()
