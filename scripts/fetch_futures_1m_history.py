"""Fetch continuous front-month index-futures 1m history (2-4 years).

AngelOne serves the current front-month future token as a CONTINUOUS rolling
1m series going back years (verified: NIFTY25AUG26FUT returns 2022 data at the
correct ~17,800 level). So one token per index yields deep 1m futures history
with no expired-contract stitching. Stored to DuckDB under a bare continuous
symbol ``<UNDERLYING>FUT`` (no expiry tag = continuous), interval 1m.

Run:
    PYTHONPATH=. .venv/bin/python scripts/fetch_futures_1m_history.py \
        --start 2022-08-15 --end 2026-08-12
"""

from __future__ import annotations

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import argparse
import os

import pyotp

from broker.angel.api.auth_api import authenticate_broker
from broker.angel.api.data import BrokerData
from database.auth_db import get_auth_token, upsert_auth
from database.historify_db import init_database, upsert_market_data
from utils.logging import get_logger

logger = get_logger(__name__)

# Current front-month future symbol per index (continuous via its token).
FRONT_MONTH = {
    "NIFTY": "NIFTY25AUG26FUT",
    "BANKNIFTY": "BANKNIFTY25AUG26FUT",
    "FINNIFTY": "FINNIFTY25AUG26FUT",
    "MIDCPNIFTY": "MIDCPNIFTY25AUG26FUT",
}


def ensure_auth() -> str | None:
    user = os.getenv("LOGIN_USERNAME", "mohit")
    cc, pin, sec = (os.getenv("ANGELONE_CLIENT_CODE"), os.getenv("ANGELONE_PIN"),
                    os.getenv("ANGELONE_TOTP_SECRET"))
    if cc and pin and sec:
        try:
            tok, feed, err = authenticate_broker(cc, pin, pyotp.TOTP(sec).now())
            if tok:
                upsert_auth(os.getenv("BROKER_API_KEY", ""), tok, "angel", feed_token=feed)
                upsert_auth(user, tok, "angel", feed_token=feed, revoke=False)
                logger.info("Refreshed AngelOne token for futures fetch")
                return tok
        except Exception:
            logger.exception("Auth refresh failed; using stored token")
    return get_auth_token(user, bypass_cache=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()

    init_database()
    tok = ensure_auth()
    if not tok:
        print("no auth token; aborting")
        return
    bd = BrokerData(tok)

    for underlying, front in FRONT_MONTH.items():
        cont_symbol = f"{underlying}FUT"
        try:
            df = bd.get_history(front, "NFO", "1m", args.start, args.end)
            if df is None or df.empty:
                print(f"{underlying}: 0 bars")
                continue
            upsert_market_data(df, cont_symbol, "NFO", "1m")
            print(f"{underlying}: {len(df):,} bars stored as {cont_symbol} "
                  f"({df['timestamp'].min()}..{df['timestamp'].max()})")
        except Exception as e:
            print(f"{underlying}: ERROR {type(e).__name__} {str(e)[:80]}")


if __name__ == "__main__":
    main()
