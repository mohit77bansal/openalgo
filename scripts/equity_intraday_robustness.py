"""Robustness pass for the promising fade signals.

A Sharpe > 3 is a red flag until proven durable. Two checks:
  1. Per-calendar-year edge — is it every year, or one lucky regime?
  2. Slippage sensitivity — the fade enters at a volatile gap open; does the
     edge survive 5 -> 15 -> 30 bps/side of slippage, or is it a fill fantasy?

Run:
    PYTHONPATH=. .venv/bin/python scripts/equity_intraday_robustness.py
"""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

from qbacktest.equity_intraday.backtest import backtest_signals, sharpe
from qbacktest.equity_intraday.data import load_panel
from qbacktest.equity_intraday.strategies import gap_down_fade, gap_up_fade

STRATS = {"gap_down_fade": gap_down_fade, "gap_up_fade": gap_up_fade}


def _stats(trades):
    if not trades:
        return 0, 0.0, 0.0
    n = len(trades)
    avg = 100 * sum(t.net_ret for t in trades) / n
    win = 100 * sum(1 for t in trades if t.net_pnl > 0) / n
    return n, avg, win


def main() -> None:
    panel = load_panel()
    rows_by_key = dict(zip(
        zip(panel["date"], panel["symbol"]),
        zip(panel["low"], panel["high"], panel["close"]),
    ))

    for name, fn in STRATS.items():
        sigs = fn(panel)
        print(f"\n===== {name} =====")

        # 1) Per-year edge (at 5 bps slippage).
        print("  by year:   year   trades  avg_trade%   win%")
        by_year: dict[int, list] = {}
        for d, day_sigs in sigs.items():
            by_year.setdefault(d.year, {})
        for year in sorted({d.year for d in sigs}):
            year_sigs = {d: s for d, s in sigs.items() if d.year == year}
            res = backtest_signals(year_sigs, rows_by_key, slip_bps=5.0)
            n, avg, win = _stats(res.trades)
            print(f"             {year}   {n:>6}   {avg:>9.3f}   {win:>5.1f}")

        # 2) Slippage sensitivity (full period).
        print("  slippage:  bps    avg_trade%   win%   sharpe")
        for bps in (5, 15, 30):
            res = backtest_signals(sigs, rows_by_key, slip_bps=float(bps))
            n, avg, win = _stats(res.trades)
            sh = sharpe(res.equity_curve, res.start_capital)
            print(f"             {bps:>3}    {avg:>9.3f}   {win:>5.1f}   {sh:>5.2f}")


if __name__ == "__main__":
    main()
