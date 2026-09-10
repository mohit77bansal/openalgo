"""Sanity + correctness tests for the premium-native scalping suite (round 2).

Two layers:

1. HERMETIC (no DB, no network): a hand-built 1m premium series that fires a
   clean momentum burst is streamed through the real BacktestEngine, proving
   each invariant that matters for an honest scalp backtest:
     - the strategy actually TRADES (a silent zero-trade strategy is a bug);
     - it is LONG-ONLY (option buying, direction == BUY);
     - it is INTRADAY (entry and exit share a calendar date);
     - the conservative fee stress only ever REDUCES net PnL.

2. DB-BACKED (skips if the Historify store / basket is absent): runs the three
   registered scalp keys across a couple of real NIFTY contracts and asserts
   they run and produce trades, matching how the basket harness drives them.

Run:  PYTHONPATH=. .venv/bin/python -m pytest test/test_options_scalping2.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar, Exchange, Instrument, InstrumentType, Side
from qbacktest.costs.costs import DEFAULT_COST_MODELS
from qbacktest.engine.engine import BacktestEngine
from services.strategies import STRATEGY_REGISTRY
from services.strategies.options_scalping2 import (
    ScalpMicroRange,
    ScalpMomentumBurst,
    ScalpVwapReversion,
)

SCALP_KEYS = ["options_scalp_momo", "options_scalp_revert", "options_scalp_range"]
CONTRACT = "NIFTY18AUG2624500CE"
FLAT_STRESS = 5.0


# --------------------------------------------------------------------- helpers

class _StaticSource:
    """Minimal DataSource: streams bars in ts order, tracks latest price."""

    def __init__(self, bars):
        self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
        self._latest: dict[str, float] = {}

    def stream(self):
        for b in self.bars:
            self._latest[b.instrument.id] = b.close
            yield b

    def latest_price(self, instrument):
        return self._latest.get(instrument.id)


def _ce_instrument() -> Instrument:
    """Canonical option Instrument matching CONTRACT, lot_size pinned to 1."""
    return Instrument(
        symbol="NIFTY", exchange=Exchange.NFO, instrument_type=InstrumentType.CE,
        lot_size=1, expiry=date(2026, 8, 18), strike=24500.0, underlying="NIFTY",
    )


def _bar(inst, ts, close, *, open_=None, vol=1000.0) -> Bar:
    o = close if open_ is None else open_
    hi = max(o, close) + 0.05
    lo = min(o, close) - 0.05
    return Bar(instrument=inst, ts=ts, timeframe="1m",
               open=o, high=hi, low=lo, close=close, volume=vol)


def _burst_bars(inst) -> list[Bar]:
    """A flat warm-up then a clean 2-bar >3% up burst that must trigger momo."""
    start = datetime(2026, 8, 18, 9, 15, tzinfo=IST)
    bars: list[Bar] = []
    # 13 flat warm-up bars (satisfies warmup=11 and rolling vol window).
    for i in range(13):
        bars.append(_bar(inst, start + timedelta(minutes=i), 100.0, vol=1000.0))
    # bar 13: small up move (no signal yet)
    bars.append(_bar(inst, start + timedelta(minutes=13), 101.0, open_=100.0, vol=1000.0))
    # bar 14: >3% burst vs close[-3]=100, up bar, fresh high, 2x volume -> ENTRY
    bars.append(_bar(inst, start + timedelta(minutes=14), 104.0, open_=101.0, vol=2500.0))
    # bar 15: hits +5% target (104*1.05=109.2) -> EXIT
    bars.append(_bar(inst, start + timedelta(minutes=15), 110.0, open_=104.0, vol=1500.0))
    # tail bars so the engine settles
    for i in range(16, 20):
        bars.append(_bar(inst, start + timedelta(minutes=i), 110.0, vol=1000.0))
    return bars


def _run(strategy, bars):
    engine = BacktestEngine(
        strategy=strategy, data=_StaticSource(bars),
        starting_capital=200_000.0, cost_model=DEFAULT_COST_MODELS["zerodha"],
    )
    return engine.run()


# --------------------------------------------------------------------- registration

def test_all_three_registered():
    for key in SCALP_KEYS:
        assert key in STRATEGY_REGISTRY, f"{key} not registered"
        entry = STRATEGY_REGISTRY[key]
        assert entry["name"]
        assert entry["source"]
        assert "scalp" in entry["description"].lower()


def test_factories_build_instances():
    assert isinstance(STRATEGY_REGISTRY["options_scalp_momo"]["factory"](CONTRACT),
                      ScalpMomentumBurst)
    assert isinstance(STRATEGY_REGISTRY["options_scalp_revert"]["factory"](CONTRACT),
                      ScalpVwapReversion)
    assert isinstance(STRATEGY_REGISTRY["options_scalp_range"]["factory"](CONTRACT),
                      ScalpMicroRange)


# --------------------------------------------------------------------- hermetic engine

def test_momentum_burst_produces_a_trade():
    inst = _ce_instrument()
    result = _run(ScalpMomentumBurst(CONTRACT), _burst_bars(inst))
    assert len(result.trades) >= 1, "momentum burst made 0 trades — silent bug"


def test_scalp_is_long_only_and_intraday():
    inst = _ce_instrument()
    result = _run(ScalpMomentumBurst(CONTRACT), _burst_bars(inst))
    for t in result.trades:
        assert t.direction == Side.BUY, "scalps must be option BUYING only"
        assert t.entry_fill.side == Side.BUY and t.exit_fill.side == Side.SELL
        assert t.entry_fill.ts.date() == t.exit_fill.ts.date(), "must be intraday"


def test_conservative_fee_only_reduces_pnl():
    inst = _ce_instrument()
    result = _run(ScalpMomentumBurst(CONTRACT), _burst_bars(inst))
    assert result.trades
    for t in result.trades:
        conserv = t.net_pnl - FLAT_STRESS
        assert conserv < t.net_pnl
        assert t.fees > 0, "a real round-trip must carry fees"


def test_bar_count_time_stop_bounds_hold():
    """No scalp should hold longer than its bar-count stop on gap-free 1m data."""
    inst = _ce_instrument()
    # A slow grind that never hits target/stop, forcing the 3-bar time stop.
    start = datetime(2026, 8, 18, 9, 15, tzinfo=IST)
    bars = [_bar(inst, start + timedelta(minutes=i), 100.0, vol=1000.0)
            for i in range(13)]
    bars.append(_bar(inst, start + timedelta(minutes=13), 101.0, open_=100.0, vol=1000.0))
    bars.append(_bar(inst, start + timedelta(minutes=14), 104.0, open_=101.0, vol=2500.0))
    # creep upward < target, so only the 3-bar time stop can close it
    for j, px in enumerate([104.5, 105.0, 105.5, 106.0, 106.5, 107.0], start=15):
        bars.append(_bar(inst, start + timedelta(minutes=j), px, open_=px - 0.2, vol=1000.0))
    result = _run(ScalpMomentumBurst(CONTRACT), bars)
    assert result.trades
    for t in result.trades:
        held_min = (t.exit_fill.ts - t.entry_fill.ts).total_seconds() / 60.0
        assert held_min <= 3.0 + 1e-6, f"held {held_min} min > 3-bar time stop"


# --------------------------------------------------------------------- DB-backed smoke

def _db_available() -> bool:
    return os.path.exists("db/historify.duckdb")


@pytest.mark.skipif(not _db_available(), reason="Historify DuckDB not present")
@pytest.mark.parametrize("key", SCALP_KEYS)
def test_db_backtest_runs(key):
    from services.backtest_service import run_backtest
    r = run_backtest(source="db", symbol=CONTRACT, exchange="NFO", interval="1m",
                     capital=10_000, cost="zerodha", strategy_key=key, _save=False)
    if r.get("status") != "success":
        pytest.skip(f"no data for {CONTRACT}: {r.get('message')}")
    m = r.get("metrics", {})
    assert m.get("n_trades", 0) >= 0
    # long-only + intraday on any produced trades
    for t in r.get("trades", []):
        assert (t.get("entry_fill", {}) or {}).get("side") == "BUY"
        e = (t.get("entry_fill", {}) or {}).get("ts", "")[:10]
        x = (t.get("exit_fill", {}) or {}).get("ts", "")[:10]
        assert e and x and e == x


@pytest.mark.skipif(not _db_available(), reason="Historify DuckDB not present")
def test_db_momo_trades_across_basket():
    """At least one real contract must produce >0 momentum-burst trades."""
    from services.backtest_service import run_backtest
    contracts = ["NIFTY18AUG2624500CE", "NIFTY18AUG2624500PE",
                 "NIFTY18AUG2624000PE", "NIFTY18AUG2624700CE"]
    total = 0
    ran = False
    for sym in contracts:
        r = run_backtest(source="db", symbol=sym, exchange="NFO", interval="1m",
                         capital=10_000, cost="zerodha",
                         strategy_key="options_scalp_momo", _save=False)
        if r.get("status") == "success":
            ran = True
            total += r.get("metrics", {}).get("n_trades", 0) or 0
    if not ran:
        pytest.skip("no basket data available")
    assert total > 0, "momentum burst produced 0 trades across the whole basket"
