"""Backtest every intraday equity screener signal on bhavcopy history.

Loads the cached bhavcopy panel, runs each screener, simulates the picks
(open entry / close exit / intraday-low stop), and prints a comparison with
net PnL, return %, win%, Sharpe, max DD, trades, and fees.

Run:
    PYTHONPATH=. .venv/bin/python scripts/equity_intraday_scan.py \
        --capital 100000 --max-positions 5 --leverage 5
"""

from __future__ import annotations

import argparse
import warnings

warnings.filterwarnings("ignore")

import pandas as pd

from qbacktest.equity_intraday.backtest import (
    backtest_signals,
    max_drawdown_pct,
    sharpe,
)
from qbacktest.equity_intraday.data import load_panel, trading_days
from qbacktest.equity_intraday.strategies import ALL_STRATEGIES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--leverage", type=float, default=5.0)
    args = ap.parse_args()

    panel = load_panel()
    days = trading_days(panel)
    n_symbols = panel["symbol"].nunique()
    print(f"panel: {len(panel):,} rows | {n_symbols} symbols | "
          f"{len(days)} trading days ({days[0]} .. {days[-1]})\n")

    # (date, symbol) -> (low, high, close) for exit resolution. Vectorized dict
    # build — a pd.Series per row over 1M+ rows is the backtest bottleneck.
    rows_by_key = dict(zip(
        zip(panel["date"], panel["symbol"]),
        zip(panel["low"], panel["high"], panel["close"]),
    ))

    # avg_trade% and payoff are the leverage-independent EDGE; ret%/sharpe/DD
    # are the portfolio result under fixed sizing.
    hdr = (f"{'signal':<20}{'trades':>7}{'avg_trade%':>11}{'win%':>7}{'payoff':>8}"
           f"{'ret%':>8}{'sharpe':>8}{'maxDD%':>8}")
    print(hdr)
    print("-" * len(hdr))

    ranked = []
    for name, fn in ALL_STRATEGIES.items():
        sigs = fn(panel)
        res = backtest_signals(
            sigs, rows_by_key,
            start_capital=args.capital,
            max_positions=args.max_positions,
            leverage=args.leverage,
        )
        if res.n_trades == 0:
            print(f"{name:<20}{0:>7}{'—':>11}{'—':>7}{'—':>8}{'—':>8}{'—':>8}{'—':>8}")
            continue
        sh = sharpe(res.equity_curve, res.start_capital)
        dd = max_drawdown_pct(res.equity_curve, res.start_capital)
        ranked.append((name, res, sh, dd))
        print(f"{name:<20}{res.n_trades:>7}{res.avg_trade_ret_pct:>11.3f}"
              f"{100*res.win_rate:>7.1f}{res.payoff_ratio:>8.2f}"
              f"{res.total_return_pct:>8.1f}{sh:>8.2f}{dd:>8.1f}")

    print("\nModel: enter at OPEN, exit at CLOSE, stop at intraday LOW/HIGH. Fixed sizing "
          f"(notional = capital*lev/max_pos = Rs {args.capital*args.leverage/args.max_positions:,.0f}/trade).")
    print("avg_trade% = mean net return per trade on notional (leverage-independent EDGE).")
    print("Signals use only open+gap+prior-day features (no look-ahead on today's H/L/C/vol).")


if __name__ == "__main__":
    main()
