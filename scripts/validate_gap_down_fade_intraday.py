"""Validate gap_down_fade fills against REAL 1m intraday bars.

The 4-year edge was measured on a daily-bar proxy (enter at open, exit at close,
stop when the day's low pierced the 2% level, filled exactly at the stop price).
That proxy makes three optimistic assumptions this script tests against actual
1m data on the real signal days:

  1. Entry — is the bhavcopy "open" fillable? Compare to the 09:15 1m bar and
     measure realised entry slippage.
  2. Stop — does the 2% stop fill AT the stop price, or does a 1m bar gap
     THROUGH it (fill worse)? This is the main risk on event-driven gaps.
  3. Exit — square off at the 15:15 1m close (real MIS cutoff), not the 15:30
     daily close the proxy used.

For each signal it reconstructs the realistic trade from 1m bars and compares
aggregate edge (avg net return / win% ) proxy-vs-real. 1m frames are cached to
``data/intraday_1m_cache/`` so reruns don't refetch.

Run:
    PYTHONPATH=. .venv/bin/python scripts/validate_gap_down_fade_intraday.py \
        --start 2026-02-01 --end 2026-08-12
"""

from __future__ import annotations

import argparse
import time
import warnings
from datetime import date, datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd

from broker.angel.api.data import BrokerData
from database.auth_db import get_auth_token
from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side
from qbacktest.equity_intraday.data import load_panel
from qbacktest.equity_intraday.strategies import gap_down_fade

CACHE = Path("data/intraday_1m_cache")
STOP_PCT = 0.02
SLIP_BPS = 5.0
SQUARE_OFF = (15, 15)


def _fetch_1m(bd: BrokerData, symbol: str, d: date, pace: float) -> pd.DataFrame | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    fp = CACHE / f"{symbol}_{d.isoformat()}.parquet"
    if fp.exists():
        try:
            return pd.read_parquet(fp)   # cache hit — no network, no pacing
        except Exception:
            pass
    try:
        df = bd.get_history(symbol, "NSE", "1m", d.isoformat(), d.isoformat())
    except Exception:
        df = None
    time.sleep(pace)                     # pace only real network calls
    if df is None or df.empty:
        return None
    df.to_parquet(fp)
    return df


def _fill_costs(cm: CostModel, price: float, qty: int, side: Side, d: date) -> float:
    return cm.compute_fill(
        exchange=Exchange.NSE, instrument_type=InstrumentType.EQ,
        product=ProductType.INTRADAY, side=side, quantity=qty, price=price, trade_date=d,
    ).total


def _to_ist_minute(ts) -> tuple[int, int]:
    # BrokerData returns epoch SECONDS (int64). Parse as UTC then convert to IST.
    dt = pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert("Asia/Kolkata")
    return dt.hour, dt.minute


def reconstruct(df: pd.DataFrame, d: date, cm: CostModel, notional: float) -> dict | None:
    """Realistic long gap-down-fade trade from 1m bars."""
    df = df.sort_values("timestamp").reset_index(drop=True)
    if len(df) < 5:
        return None
    entry = float(df.iloc[0]["open"])          # 09:15 first-minute open
    if entry <= 0:
        return None
    qty = int(notional // entry)
    if qty <= 0:
        return None
    stop = entry * (1 - STOP_PCT)
    slip = SLIP_BPS / 10_000.0

    exit_px, reason = None, "square_off"
    for _, b in df.iterrows():
        h, m = _to_ist_minute(b["timestamp"])
        if (h, m) < (9, 15):
            continue
        if float(b["low"]) <= stop:
            # Gap-through: if the bar opened below the stop, you fill at the open,
            # not the (better) stop price. Otherwise fill at the stop.
            bo = float(b["open"])
            exit_px = min(stop, bo)
            reason = "stop"
            break
        if (h, m) >= SQUARE_OFF:
            exit_px = float(b["close"])
            reason = "square_off"
            break
    if exit_px is None:
        exit_px = float(df.iloc[-1]["close"])

    eff_entry = entry * (1 + slip)              # long pays up on entry
    eff_exit = exit_px * (1 - slip)             # sells low on exit
    gross = (eff_exit - eff_entry) * qty
    costs = (_fill_costs(cm, eff_entry, qty, Side.BUY, d)
             + _fill_costs(cm, eff_exit, qty, Side.SELL, d))
    net = gross - costs
    return {
        "entry": entry, "stop": stop, "exit": exit_px, "reason": reason,
        "qty": qty, "net": net, "net_ret": net / (qty * entry),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--leverage", type=float, default=5.0)
    ap.add_argument("--pace", type=float, default=0.3)
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    notional = args.capital * args.leverage / args.max_positions
    cm = CostModel(brokerage=zerodha_cost_model())

    panel = load_panel()
    # (date, symbol) -> proxy OHLC for the daily comparison.
    proxy = {(r.date, r.symbol): (r.open, r.high, r.low, r.close)
             for r in panel.itertuples()}

    sigs = gap_down_fade(panel)
    signals = []
    for d in sorted(sigs):
        if start <= d <= end:
            for s in sigs[d][:args.max_positions]:
                signals.append(s)
    print(f"gap_down_fade signals {start}..{end}: {len(signals)} "
          f"(top-{args.max_positions}/day)\n")

    bd = BrokerData(get_auth_token("mohit", bypass_cache=True))

    real_rets, proxy_rets = [], []
    real_wins = proxy_wins = 0
    stop_through = 0
    entry_slips = []
    fetched = missing = 0

    for i, sig in enumerate(signals):
        df = _fetch_1m(bd, sig.symbol, sig.date, args.pace)
        if df is None:
            missing += 1
            continue
        fetched += 1
        r = reconstruct(df, sig.date, cm, notional)
        if r is None:
            continue
        real_rets.append(r["net_ret"])
        real_wins += 1 if r["net"] > 0 else 0
        # gap-through detection: stop filled below the intended stop price
        if r["reason"] == "stop" and r["exit"] < r["stop"] - 1e-6:
            stop_through += 1
        # entry slippage vs bhavcopy open
        po = proxy.get((sig.date, sig.symbol))
        if po:
            b_open, b_high, b_low, b_close = po
            if b_open > 0:
                entry_slips.append((r["entry"] - b_open) / b_open)
            # proxy trade
            p_stop = b_open * (1 - STOP_PCT)
            p_exit = p_stop if b_low <= p_stop else b_close
            qty = int(notional // b_open) if b_open > 0 else 0
            if qty > 0:
                slip = SLIP_BPS / 10_000.0
                ee = b_open * (1 + slip); xx = p_exit * (1 - slip)
                p_gross = (xx - ee) * qty
                p_costs = (_fill_costs(cm, ee, qty, Side.BUY, sig.date)
                           + _fill_costs(cm, xx, qty, Side.SELL, sig.date))
                p_net = p_gross - p_costs
                proxy_rets.append(p_net / (qty * b_open))
                proxy_wins += 1 if p_net > 0 else 0
        if args.pace and not df.equals(df):  # never true; keeps linter calm
            pass
        time.sleep(args.pace if fetched % 1 == 0 else 0)

    def avg(x):
        return 100 * sum(x) / len(x) if x else 0.0

    n = len(real_rets)
    print(f"fetched {fetched} | missing 1m {missing}\n")
    print(f"{'metric':<28}{'PROXY (daily)':>16}{'REAL (1m)':>14}")
    print("-" * 58)
    print(f"{'trades':<28}{len(proxy_rets):>16}{n:>14}")
    print(f"{'avg net return/trade %':<28}{avg(proxy_rets):>16.3f}{avg(real_rets):>14.3f}")
    print(f"{'win %':<28}{100*proxy_wins/max(len(proxy_rets),1):>16.1f}"
          f"{100*real_wins/max(n,1):>14.1f}")
    print(f"\nentry slippage vs bhavcopy open: mean {avg(entry_slips):.3f}% "
          f"(n={len(entry_slips)})")
    print(f"stops that gapped THROUGH the 2% level: {stop_through}/{n} "
          f"({100*stop_through/max(n,1):.1f}%)")
    print(f"\nreal model: entry=09:15 1m open, exit=15:15 close or stop-through, "
          f"slippage {SLIP_BPS}bps/side")


if __name__ == "__main__":
    main()
