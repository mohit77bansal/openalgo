"""Backtest every multi-leg structure across all expiries in the premium DB.

Loads the near-ATM NIFTY chain per expiry, runs each structure once per
trading day, and prints a comparison table: trades, net PnL, win%, avg
credit/debit, fees, and estimated margin. Honest about the small sample.

Run:
    PYTHONPATH=. .venv/bin/python scripts/multileg_scan.py
"""

from __future__ import annotations

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import warnings

warnings.filterwarnings("ignore")

from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.multileg.backtest import load_sessions, run_strategy
from qbacktest.multileg.strategies import ALL_STRATEGIES

DB = "db/historify.duckdb"
SPOT = "NIFTY25AUG26FUT"
EXPIRIES = ["11AUG26", "18AUG26"]
STRIKE_LO, STRIKE_HI = 23800, 25200


def main() -> None:
    cost_model = CostModel(brokerage=zerodha_cost_model())

    # Load sessions for every expiry, tagged so we can report coverage.
    all_sessions = []
    for exp in EXPIRIES:
        s = load_sessions(
            DB, exp, spot_symbol=SPOT,
            strike_lo=STRIKE_LO, strike_hi=STRIKE_HI, min_bars=1200,
        )
        print(f"expiry {exp}: {len(s)} trading days, "
              f"strikes CE={len(s[0].ce) if s else 0} PE={len(s[0].pe) if s else 0}")
        all_sessions.extend(s)
    print(f"total sessions: {len(all_sessions)}\n")

    hdr = (f"{'structure':<20}{'trades':>7}{'net_pnl':>11}{'win%':>7}"
           f"{'avg/trade':>11}{'avg_prem':>10}{'fees':>9}{'margin':>10}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for name, fn in ALL_STRATEGIES.items():
        trades = run_strategy(all_sessions, fn, cost_model)
        n = len(trades)
        if n == 0:
            print(f"{name:<20}{0:>7}{'—':>11}{'—':>7}{'—':>11}{'—':>10}{'—':>9}{'—':>10}")
            continue
        net = sum(t.net_pnl for t in trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        fees = sum(t.costs for t in trades)
        avg_prem = sum(t.net_premium for t in trades) / n
        margin = max(t.margin_est for t in trades)
        avg = net / n
        rows.append((name, n, net, 100 * wins / n, avg, avg_prem, fees, margin))
        print(f"{name:<20}{n:>7}{net:>11.0f}{100*wins/n:>7.1f}"
              f"{avg:>11.0f}{avg_prem:>10.0f}{fees:>9.0f}{margin:>10.0f}")

    print("\nnet_premium sign: +debit paid / -credit received")
    print("Per-day trade detail (structure | day | entry->exit | net):")
    for name, fn in ALL_STRATEGIES.items():
        trades = run_strategy(all_sessions, fn, cost_model)
        for t in trades:
            print(f"  {name:<18} {t.day} {t.exit_reason:<10} "
                  f"prem={t.net_premium:>8.0f} gross={t.gross_pnl:>8.0f} "
                  f"cost={t.costs:>6.0f} net={t.net_pnl:>8.0f}")


if __name__ == "__main__":
    main()
