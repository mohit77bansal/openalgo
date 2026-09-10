"""Correctness tests for the multi-leg option backtester.

Two invariants pin the P&L math so a bug can't silently fake a profitable
spread:

1. **Mirror invariant.** A BUY structure and the identical SELL structure must
   have exactly opposite gross P&L on any price path. If they don't, the
   sign handling in ``mtm`` is wrong.
2. **Hand-computed P&L.** A single long call over a known price move must equal
   (exit - entry) * lots * lot_size to the rupee, minus modelled costs.
"""

from __future__ import annotations

from datetime import date

from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.multileg.backtest import (
    Leg,
    NIFTY_LOT,
    Session,
    simulate_structure,
)

# Bars at 09:15, 09:16, 09:17 IST on 2026-08-11 (epoch seconds, IST=UTC+5:30).
# 2026-08-11 09:15 IST == 2026-08-11 03:45 UTC == 1786** ; compute directly.
import datetime as _dt

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _ts(h, m):
    return int(_dt.datetime(2026, 8, 11, h, m, tzinfo=IST).timestamp())


def _session(ce_path, pe_path):
    ts = [_ts(9, 15), _ts(9, 16), _ts(9, 17)]
    spot = [24500.0, 24500.0, 24500.0]
    return Session(
        day=date(2026, 8, 11),
        ts=ts,
        spot=spot,
        ce={24500: ce_path},
        pe={24500: pe_path},
    )


def test_mirror_invariant_long_equals_negative_short():
    """Long straddle gross == -(short straddle gross) on the same path."""
    ce = [100.0, 130.0, 120.0]
    pe = [90.0, 70.0, 60.0]
    s = _session(ce, pe)
    cm = CostModel(brokerage=zerodha_cost_model())

    long_legs = (Leg(24500, "CE", "BUY"), Leg(24500, "PE", "BUY"))
    short_legs = (Leg(24500, "CE", "SELL"), Leg(24500, "PE", "SELL"))

    lr = simulate_structure(
        s, 0, long_legs, label="L", entry_reason="", target_frac=99, stop_frac=99,
        exit_i_max=2, cost_model=cm, margin_est=0,
    )
    sr = simulate_structure(
        s, 0, short_legs, label="S", entry_reason="", target_frac=99, stop_frac=99,
        exit_i_max=2, cost_model=cm, margin_est=0,
    )
    assert lr is not None and sr is not None
    # Time-exit at bar 2: CE 100->120 (+20), PE 90->60 (-30). Long net move = -10/unit.
    assert abs(lr.gross_pnl + sr.gross_pnl) < 1e-6
    assert lr.gross_pnl == (20 - 30) * NIFTY_LOT      # -10 * 75 = -750


def test_hand_computed_single_leg_pnl():
    """One long call, 100 -> 130 over the path, time-exit at last bar."""
    ce = [100.0, 110.0, 130.0]
    pe = [50.0, 50.0, 50.0]
    s = _session(ce, pe)
    cm = CostModel(brokerage=zerodha_cost_model())

    r = simulate_structure(
        s, 0, (Leg(24500, "CE", "BUY"),), label="C", entry_reason="",
        target_frac=99, stop_frac=99, exit_i_max=2, cost_model=cm, margin_est=0,
    )
    assert r is not None
    assert r.gross_pnl == (130 - 100) * NIFTY_LOT     # 30 * 75 = 2250
    assert r.net_pnl == round(r.gross_pnl - r.costs, 2)
    assert r.costs > 0                                 # entry buy + exit sell both cost


def test_target_and_stop_fire_on_credit_structure():
    """A sold straddle that decays hits its profit target, not time-exit."""
    # CE and PE both decay from 100 -> 50; seller gains (100-50)*2*75 = 7500 gross.
    ce = [100.0, 70.0, 50.0]
    pe = [100.0, 70.0, 50.0]
    s = _session(ce, pe)
    cm = CostModel(brokerage=zerodha_cost_model())

    legs = (Leg(24500, "CE", "SELL"), Leg(24500, "PE", "SELL"))
    # entry credit = 200/unit * 75 = 15000; target 25% = 3750 -> hit at bar1
    # (decay 60/unit * 75 = 4500 >= 3750).
    r = simulate_structure(
        s, 0, legs, label="SS", entry_reason="", target_frac=0.25, stop_frac=0.60,
        exit_i_max=2, cost_model=cm, margin_est=0,
    )
    assert r is not None
    assert r.exit_reason == "target"
    assert r.exit_i == 1
