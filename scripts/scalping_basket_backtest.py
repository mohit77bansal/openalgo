"""Honest scalping backtest: the 3 premium-native scalps on the REAL NIFTY basket.

Runs ``options_scalp_momo`` / ``options_scalp_revert`` / ``options_scalp_range``
across every near-ATM NIFTY option contract with real 1m premium bars
(Aug 2026, strikes 24000-25000) and prints a comparison table.

FEES DOMINATE scalping, so every strategy is reported at TWO fee levels:
  - MODEL   : the qbacktest CostModel (Zerodha: flat Rs.20/order, options STT
              0.10% on sell premium, exch txn, GST, stamp) — already net in
              ``net_pnl``.
  - CONSERV : MODEL plus a stress for the known STT understatement. We add BOTH
              a flat Rs.5/round-trip (calibrated to this basket) AND, separately,
              a true STT bump to 0.15% (extra 0.05% of each exit's sell premium),
              and report the harsher-scaling STT version as the headline CONSERV.

Per-trade edge is reported as avg net %/trade on the ~Rs.10k deployed per scalp
(the premium-native default capital_per_trade), which is the number that decides
whether a tiny-edge scalp is real.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/scalping_basket_backtest.py
"""
from dotenv import load_dotenv; load_dotenv()

import re
import time as _time
import warnings

import duckdb

warnings.filterwarnings("ignore")

from services.backtest_service import run_backtest  # noqa: E402

# Premium-native default per-trade deployed capital (see options_premium_native).
CAPITAL_PER_TRADE = 10_000.0
FLAT_STRESS_PER_RT = 5.0          # Rs./round-trip flat stress
STT_EXTRA_RATE = 0.0005           # 0.10% model -> 0.15% actual = +0.05% on sell premium

STRATS = ["options_scalp_momo", "options_scalp_revert", "options_scalp_range"]


def _strike(sym: str) -> int | None:
    m = re.match(r"^NIFTY\d{2}[A-Z]{3}\d{2}(\d+)(CE|PE)$", sym)
    return int(m.group(1)) if m else None


def _load_basket() -> list[str]:
    conn = duckdb.connect("db/historify.duckdb", read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT symbol, COUNT(*) n FROM market_data
            WHERE (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE')
              AND interval = '1m'
            GROUP BY symbol HAVING n > 1500
            ORDER BY n DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [s for s, _ in rows if (k := _strike(s)) and 24000 <= k <= 25000]


def _trade_pnls(trades: list[dict]) -> list[dict]:
    """Reconstruct per-trade gross/fees/exit-notional from JSON fills (long-only)."""
    out = []
    for t in trades:
        ef = t.get("entry_fill", {}) or {}
        xf = t.get("exit_fill", {}) or {}
        try:
            eq = min(int(ef.get("quantity", 0)), int(xf.get("quantity", 0)))
            ep = float(ef.get("price", 0.0))
            xp = float(xf.get("price", 0.0))
            fees = float(ef.get("fees", 0.0)) + float(xf.get("fees", 0.0))
        except (TypeError, ValueError):
            continue
        gross = (xp - ep) * eq
        out.append({"net": gross - fees, "exit_notional": xp * eq})
    return out


def _agg_new() -> dict:
    return {"contracts": 0, "trades": 0, "fees": 0.0,
            "net_model": 0.0, "net_flat": 0.0, "net_stt": 0.0,
            "wins_model": 0, "wins_stt": 0}


def run() -> None:
    basket = _load_basket()
    print(f"basket: {len(basket)} real NIFTY option contracts "
          f"(strikes 24000-25000, 1m premium bars, Aug 2026)\n")

    agg = {k: _agg_new() for k in STRATS}
    t0 = _time.time()
    for sym in basket:
        for sk in STRATS:
            try:
                r = run_backtest(source="db", symbol=sym, exchange="NFO",
                                 interval="1m", capital=CAPITAL_PER_TRADE,
                                 cost="zerodha", strategy_key=sk, _save=False)
            except Exception:
                continue
            if r.get("status") != "success":
                continue
            pnls = _trade_pnls(r.get("trades", []))
            if not pnls:
                continue
            a = agg[sk]
            a["contracts"] += 1
            a["trades"] += len(pnls)
            for p in pnls:
                net_model = p["net"]
                net_stt = net_model - STT_EXTRA_RATE * p["exit_notional"]
                a["net_model"] += net_model
                a["net_flat"] += net_model - FLAT_STRESS_PER_RT
                a["net_stt"] += net_stt
                a["wins_model"] += 1 if net_model > 0 else 0
                a["wins_stt"] += 1 if net_stt > 0 else 0
            m = r.get("metrics", {})
            a["fees"] += float(m.get("fees_total", 0) or 0)

    def pct_per_trade(net: float, n: int) -> float:
        return (net / n / CAPITAL_PER_TRADE * 100) if n else 0.0

    hdr = (f"{'strategy':<22}{'ctr':>4}{'trades':>7}{'net_model':>11}"
           f"{'net_+5':>10}{'net_STT':>10}{'win%':>7}{'%/trade':>9}")
    print("MODEL = qbacktest CostModel. net_+5 = flat Rs.5/RT stress. "
          "net_STT = STT bumped 0.10%->0.15% (headline CONSERV).")
    print("%/trade = avg net %/trade on ~Rs.10k deployed, under net_STT.\n")
    print(hdr)
    print("-" * len(hdr))
    for sk in STRATS:
        a = agg[sk]
        n = a["trades"]
        win_stt = (a["wins_stt"] / n * 100) if n else 0.0
        print(f"{sk:<22}{a['contracts']:>4}{n:>7}{a['net_model']:>11.0f}"
              f"{a['net_flat']:>10.0f}{a['net_stt']:>10.0f}{win_stt:>7.1f}"
              f"{pct_per_trade(a['net_stt'], n):>9.3f}")

    print(f"\nelapsed: {_time.time() - t0:.0f}s")
    print("\nPer-trade edge under the headline conservative (STT) fee, in Rs.:")
    for sk in STRATS:
        a = agg[sk]
        n = a["trades"]
        edge = (a["net_stt"] / n) if n else 0.0
        model_edge = (a["net_model"] / n) if n else 0.0
        print(f"  {sk:<22} model Rs.{model_edge:+7.1f}/trade   "
              f"conserv Rs.{edge:+7.1f}/trade   ({n} trades)")


if __name__ == "__main__":
    run()
