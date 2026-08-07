"""Backtest service — bridges OpenAlgo to the vendored aladin `qbacktest` engine.

This is the thin, broker-agnostic orchestration layer (same role as every other
`services/*_service.py`): a blueprint calls in, this runs the event-driven
backtest engine and returns a JSON-serialisable result.

Design notes
------------
- The heavy work is isolated in `_run_engine()`. On the dev server (`uv run
  app.py`, standard threading) it is safe to call inline. In production
  (gunicorn + eventlet, single worker) a CPU-bound backtest must NOT run in the
  request path — move `_run_engine()` to a subprocess/process-pool worker there.
  The seam is deliberately one function so that change is localised.
- This first cut runs a *self-contained synthetic* backtest so the end-to-end
  loop (request -> engine -> equity curve) is provable with no seeded market
  data. Wiring real history (OpenAlgo `/history` or the Historify DuckDB store)
  is the next step and only changes the DataSource passed to the engine.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# Cost models available to the demo (keys of qbacktest DEFAULT_COST_MODELS).
DEMO_COST_MODELS = ("zerodha", "upstox", "fyers", "zero")
DEFAULT_CAPITAL = 1_000_000.0


# --------------------------------------------------------------------------- #
# JSON sanitisation — the engine returns dataclasses / datetimes / enums.
# --------------------------------------------------------------------------- #
def _json_safe(obj: Any) -> Any:
    """Recursively convert engine output into JSON-serialisable primitives."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        # guard against NaN/inf which are not valid JSON
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return str(obj)


# --------------------------------------------------------------------------- #
# Self-contained synthetic data + strategy (no external market data required).
# --------------------------------------------------------------------------- #
def _build_synthetic_source(symbol: str, n_bars: int) -> Any:
    """A deterministic daily bar series with a trend + oscillation."""
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar, Exchange, Instrument, InstrumentType

    inst = Instrument(
        symbol=symbol, exchange=Exchange.NSE,
        instrument_type=InstrumentType.EQ, lot_size=1,
    )
    base = 25_000.0
    bars: list[Any] = []
    d = date(2024, 1, 1)
    made = 0
    i = 0
    while made < n_bars:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:  # skip weekends
            continue
        # deterministic: gentle uptrend + oscillation, no randomness
        px = base * (1.0 + 0.0008 * i + 0.02 * math.sin(i / 6.0))
        bars.append(Bar(
            instrument=inst,
            ts=datetime.combine(d, time(15, 30), tzinfo=IST),
            timeframe="1d",
            open=px, high=px * 1.004, low=px * 0.996, close=px * 1.001,
            volume=1000,
        ))
        made += 1
        i += 1

    class _StaticSource:
        def __init__(self, bars: list[Any]):
            self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
            self._latest: dict[str, float] = {}

        def stream(self):
            for b in self.bars:
                self._latest[b.instrument.id] = b.close
                yield b

        def latest_price(self, instrument: Any) -> float | None:
            return self._latest.get(instrument.id)

    return _StaticSource(bars)


def _make_strategy(symbol: str, window: int) -> Any:
    """A simple SMA-momentum strategy: long above the SMA, flat below."""
    from qbacktest.engine.strategy import Strategy

    class DemoMomentum(Strategy):
        name = "DemoMomentum"

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._long = False

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != symbol:
                return
            self._closes.append(bar.close)
            if len(self._closes) <= window:
                return
            sma = sum(self._closes[-window:]) / window
            inst = ctx.equity_(symbol)
            if bar.close > sma and not self._long:
                ctx.buy(inst, quantity=1)
                self._long = True
            elif bar.close < sma and self._long:
                ctx.sell(inst, quantity=1)
                self._long = False

    return DemoMomentum()


# --------------------------------------------------------------------------- #
# Engine invocation — the one seam to offload to a worker in production.
# --------------------------------------------------------------------------- #
def _run_engine(symbol: str, capital: float, cost: str, n_bars: int, window: int) -> Any:
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.engine.engine import BacktestEngine

    source = _build_synthetic_source(symbol, n_bars)
    engine = BacktestEngine(
        strategy=_make_strategy(symbol, window),
        data=source,
        starting_capital=capital,
        cost_model=DEFAULT_COST_MODELS[cost],
    )
    return engine.run()


def run_demo_backtest(
    symbol: str = "NIFTY",
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    n_bars: int = 120,
    window: int = 8,
) -> dict[str, Any]:
    """Run the self-contained demo backtest and return a JSON-safe result.

    Returns a dict: status, strategy, symbol, capital, cost_model, metrics,
    equity_curve ([[iso_ts, equity], ...]), trades, n_bars.
    """
    if cost not in DEMO_COST_MODELS:
        cost = "zerodha"
    capital = float(capital)
    if not math.isfinite(capital) or capital <= 0:
        capital = DEFAULT_CAPITAL

    try:
        result = _run_engine(symbol, capital, cost, n_bars, window)
    except Exception:
        logger.exception("demo backtest failed")
        return {"status": "error", "message": "backtest engine failed — see logs"}

    equity_curve = [[ts.isoformat(), _json_safe(eq)] for ts, eq in result.equity_curve]
    trades = _json_safe(getattr(result, "trades", []))

    return {
        "status": "success",
        "strategy": "DemoMomentum",
        "symbol": symbol,
        "capital": capital,
        "cost_model": cost,
        "n_bars": n_bars,
        "metrics": _json_safe(getattr(result, "metrics", {})),
        "equity_curve": equity_curve,
        "trades": trades,
    }
