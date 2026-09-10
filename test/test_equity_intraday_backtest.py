"""Correctness tests for the intraday equity backtest engine.

Pins the P&L math (long/short, stop-out, slippage, costs) with hand-computed
values so a sign or fill bug can't silently manufacture edge.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from qbacktest.equity_intraday.backtest import Signal, backtest_signals


def _row(low, high, close):  # noqa
    return (low, high, close)  # backtest expects a (low, high, close) tuple


def test_long_exit_at_close_no_stop():
    d = date(2026, 8, 12)
    sig = Signal(date=d, symbol="X", direction=+1, entry=100.0, stop_pct=0.02)
    rows = {(d, "X"): _row(low=99.0, high=105.0, close=104.0)}  # low 99 > stop 98 → no stop
    res = backtest_signals(
        {d: [sig]}, rows, start_capital=100_000, max_positions=1,
        leverage=1.0, slip_bps=0.0,
    )
    t = res.trades[0]
    assert t.exit_reason == "close"
    # notional 100k/1*1 = 100k -> qty = 1000 @100. gross = (104-100)*1000 = 4000.
    assert t.qty == 1000
    assert t.gross_pnl == 4000.0
    assert t.net_pnl == round(4000.0 - t.costs, 2)


def test_long_stops_out_on_low():
    d = date(2026, 8, 12)
    sig = Signal(date=d, symbol="X", direction=+1, entry=100.0, stop_pct=0.02)
    rows = {(d, "X"): _row(low=97.0, high=101.0, close=100.5)}  # low 97 <= stop 98 → stop
    res = backtest_signals(
        {d: [sig]}, rows, start_capital=100_000, max_positions=1,
        leverage=1.0, slip_bps=0.0,
    )
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit == 98.0                              # stop price
    assert t.gross_pnl == (98.0 - 100.0) * 1000        # -2000


def test_short_stops_out_on_high():
    d = date(2026, 8, 12)
    sig = Signal(date=d, symbol="X", direction=-1, entry=100.0, stop_pct=0.02)
    rows = {(d, "X"): _row(low=99.0, high=103.0, close=101.0)}  # high 103 >= stop 102 → stop
    res = backtest_signals(
        {d: [sig]}, rows, start_capital=100_000, max_positions=1,
        leverage=1.0, slip_bps=0.0,
    )
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit == 102.0
    assert t.gross_pnl == (100.0 - 102.0) * 1000       # short: -2000


def test_slippage_is_adverse_both_sides():
    d = date(2026, 8, 12)
    sig = Signal(date=d, symbol="X", direction=+1, entry=100.0, stop_pct=0.05)
    rows = {(d, "X"): _row(low=99.0, high=110.0, close=110.0)}  # no stop, exit close 110
    no_slip = backtest_signals({d: [sig]}, rows, start_capital=100_000,
                               max_positions=1, leverage=1.0, slip_bps=0.0).trades[0]
    with_slip = backtest_signals({d: [sig]}, rows, start_capital=100_000,
                                 max_positions=1, leverage=1.0, slip_bps=50.0).trades[0]
    # 50 bps: entry 100 -> 100.5, exit 110 -> 109.45. Slippage must reduce gross.
    assert with_slip.gross_pnl < no_slip.gross_pnl
