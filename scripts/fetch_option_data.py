"""One-shot script: fetch 1m option data for ATM +/- 10 strikes across all 4 indices."""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///db/openalgo.db")

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from datetime import date
from database.symbol import db_session, SymToken
from database.auth_db import get_auth_token, upsert_auth
from database.historify_db import upsert_market_data
from broker.angel.api.data import BrokerData

MONTH_NUM = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
ATM = {"NIFTY": 24500, "BANKNIFTY": 52500, "FINNIFTY": 24000, "MIDCPNIFTY": 13500}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}
RANGE = 10


def get_auth():
    api_key = os.getenv("BROKER_API_KEY", "")
    token = get_auth_token(api_key)
    if token:
        return token
    import pyotp
    from broker.angel.api.auth_api import authenticate_broker
    totp = pyotp.TOTP(os.getenv("ANGELONE_TOTP_SECRET", "")).now()
    token, _, err = authenticate_broker(
        os.getenv("ANGELONE_CLIENT_CODE", ""),
        os.getenv("ANGELONE_PIN", ""),
        totp,
    )
    if token:
        upsert_auth(api_key, token, "angel")
        return token
    print(f"Auth failed: {err}")
    sys.exit(1)


def collect_symbols():
    today = date.today()
    symbols = []
    for underlying in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]:
        atm = ATM[underlying]
        step = STRIKE_STEP[underlying]
        lo, hi = atm - RANGE * step, atm + RANGE * step

        rows = db_session.query(SymToken).filter(
            SymToken.exchange == "NFO",
            SymToken.symbol.like(f"{underlying}%"),
            SymToken.symbol.notlike(f"{underlying}%FUT"),
        ).all()

        expiries = set()
        for r in rows:
            m = re.match(rf"^{underlying}(\d{{2}})([A-Z]{{3}})(\d{{2}})(\d+)(CE|PE)$", r.symbol)
            if m:
                dd, mon, yy, strike, opt = m.groups()
                exp = date(2000 + int(yy), MONTH_NUM[mon], int(dd))
                if exp >= today:
                    expiries.add(exp)

        nearest = sorted(expiries)[:2]
        for r in rows:
            m = re.match(rf"^{underlying}(\d{{2}})([A-Z]{{3}})(\d{{2}})(\d+)(CE|PE)$", r.symbol)
            if m:
                dd, mon, yy, strike, opt = m.groups()
                exp = date(2000 + int(yy), MONTH_NUM[mon], int(dd))
                if exp in nearest and lo <= int(strike) <= hi:
                    symbols.append((r.symbol, r.exchange, str(r.token)))

    db_session.remove()
    return symbols


def main():
    auth_token = get_auth()
    bd = BrokerData(auth_token)
    symbols = collect_symbols()
    print(f"Fetching 1m data for {len(symbols)} option symbols...")

    success = failed = no_data = 0
    for i, (sym, exch, token) in enumerate(symbols):
        try:
            result = bd.get_history(sym, exch, "1m", "2026-08-04 09:15", "2026-08-10 15:30")
            if isinstance(result, pd.DataFrame) and not result.empty:
                upsert_market_data(result, sym, exch, "1m")
                success += 1
            else:
                no_data += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f"  ERROR {sym}: {str(e)[:100]}")

        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{len(symbols)} (ok={success}, empty={no_data}, fail={failed})")
        time.sleep(0.5)

    print(f"\nDone: {success} with data, {no_data} empty, {failed} errors out of {len(symbols)}")


if __name__ == "__main__":
    main()
