"""Backtest service — bridges OpenAlgo to the vendored aladin `qbacktest` engine.

Thin, broker-agnostic orchestration (same role as every other
`services/*_service.py`): a blueprint calls in, this runs the event-driven
backtest engine and returns a JSON-serialisable result.

Data sources (the `source` argument):
- "demo": self-contained deterministic synthetic bars — no data required.
- "db"  : real OHLCV from the Historify DuckDB store via
          `database.historify_db.get_ohlcv` (populated from AngelOne). Returns
          a graceful "no data" result until bars have been ingested.

Scope for now: NIFTY 50 index (NSE) and NIFTY futures (NFO).

Production note: the heavy call is isolated in `_run_engine()`. On the dev
server (standard threading) it is safe inline; under gunicorn+eventlet move that
one function to a subprocess/process-pool worker so it never blocks the request
path.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

DEMO_COST_MODELS = ("zerodha", "upstox", "fyers", "zero")
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_SYMBOL = "NIFTY"

# OpenAlgo interval -> qbacktest Bar.timeframe label.
_INTERVAL_TF = {"D": "1d", "1d": "1d", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}


# --------------------------------------------------------------------------- #
# JSON sanitisation
# --------------------------------------------------------------------------- #
def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
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
# Instrument mapping (NIFTY index / futures / equity)
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_FUT_RE = __import__("re").compile(r"(\d{2})([A-Z]{3})(\d{2})FUT$")


def _parse_fut_expiry(symbol: str) -> date | None:
    """Parse an F&O expiry from an OpenAlgo future symbol, e.g. NIFTY25AUG26FUT."""
    m = _FUT_RE.search((symbol or "").upper())
    if not m:
        return None
    dd, mon, yy = m.groups()
    return date(2000 + int(yy), _MONTHS[mon], int(dd))


def _instrument_for(symbol: str, exchange: str) -> Any:
    from qbacktest.core.types import Exchange, Instrument, InstrumentType

    ex = (exchange or "NSE").upper()
    if ex in ("NSE_INDEX", "BSE_INDEX", "INDEX"):
        qex = Exchange.BSE if ex == "BSE_INDEX" else Exchange.NSE
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.INDEX, lot_size=1)
    if ex in ("NFO", "BFO"):
        qex = Exchange.BFO if ex == "BFO" else Exchange.NFO
        # NIFTY futures lot size (current); refine per-expiry via lot_sizes later.
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.FUT,
                          lot_size=75, expiry=_parse_fut_expiry(symbol))
    qex = getattr(Exchange, ex, Exchange.NSE)
    return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.EQ, lot_size=1)


class _StaticSource:
    """A qbacktest DataSource backed by an in-memory list of Bars."""

    def __init__(self, bars: list[Any]):
        self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
        self._latest: dict[str, float] = {}

    def stream(self):
        for b in self.bars:
            self._latest[b.instrument.id] = b.close
            yield b

    def latest_price(self, instrument: Any) -> float | None:
        return self._latest.get(instrument.id)


# --------------------------------------------------------------------------- #
# "demo" — deterministic synthetic bars
# --------------------------------------------------------------------------- #
def _build_synthetic_source(symbol: str, exchange: str, n_bars: int) -> _StaticSource:
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar

    inst = _instrument_for(symbol, exchange)
    base = 25_000.0
    bars: list[Any] = []
    d, made, i = date(2024, 1, 1), 0, 0
    while made < n_bars:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:
            continue
        px = base * (1.0 + 0.0008 * i + 0.02 * math.sin(i / 6.0))
        bars.append(Bar(
            instrument=inst, ts=datetime.combine(d, time(15, 30), tzinfo=IST),
            timeframe="1d", open=px, high=px * 1.004, low=px * 0.996, close=px * 1.001, volume=1000,
        ))
        made += 1
        i += 1
    return _StaticSource(bars)


# --------------------------------------------------------------------------- #
# "db" — real OHLCV from the Historify DuckDB store
# --------------------------------------------------------------------------- #
def _build_db_source(symbol: str, exchange: str, interval: str, start: str | None, end: str | None):
    """Returns (_StaticSource, n_bars) or raises ValueError('no data ...')."""
    from database.historify_db import get_ohlcv
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar

    start_ts = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_ts = int(datetime.fromisoformat(end).timestamp()) if end else None
    df = get_ohlcv(symbol, exchange, interval, start_timestamp=start_ts, end_timestamp=end_ts)
    if df is None or len(df) == 0:
        raise ValueError(
            f"no {interval} data for {symbol}/{exchange} in the Historify store — "
            f"ingest it from AngelOne first"
        )

    inst = _instrument_for(symbol, exchange)
    tf = _INTERVAL_TF.get(interval, "1d")
    bars: list[Any] = []
    for row in df.itertuples(index=False):
        ts = datetime.fromtimestamp(int(row.timestamp), tz=timezone.utc).astimezone(IST)
        bars.append(Bar(
            instrument=inst, ts=ts, timeframe=tf,
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(getattr(row, "volume", 0) or 0),
        ))
    return _StaticSource(bars), len(bars)


# --------------------------------------------------------------------------- #
# Strategy (simple SMA momentum — works on index/futures/equity series alike)
# --------------------------------------------------------------------------- #
def _make_strategy(symbol: str, window: int) -> Any:
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
            # Trade the instrument we are actually fed (index/future/equity),
            # not a synthesized equity — otherwise orders reference an
            # instrument with no price stream and never fill. Quantity must be a
            # lot-size multiple for derivatives (the engine rejects otherwise),
            # so trade exactly one lot.
            inst = bar.instrument
            qty = max(getattr(inst, "lot_size", 1) or 1, 1)
            if bar.close > sma and not self._long:
                ctx.buy(inst, quantity=qty)
                self._long = True
            elif bar.close < sma and self._long:
                ctx.sell(inst, quantity=qty)
                self._long = False

    return DemoMomentum()


# --------------------------------------------------------------------------- #
# Engine invocation (the one seam to offload to a worker in production)
# --------------------------------------------------------------------------- #
def _run_engine(source_obj: Any, symbol: str, capital: float, cost: str, window: int) -> Any:
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.engine.engine import BacktestEngine

    engine = BacktestEngine(
        strategy=_make_strategy(symbol, window),
        data=source_obj,
        starting_capital=capital,
        cost_model=DEFAULT_COST_MODELS[cost],
    )
    return engine.run()


def run_backtest(
    source: str = "demo",
    symbol: str = DEFAULT_SYMBOL,
    exchange: str = "NSE",
    interval: str = "D",
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    window: int = 8,
    n_bars: int = 120,
) -> dict[str, Any]:
    """Run a backtest and return a JSON-safe result (see module docstring)."""
    if cost not in DEMO_COST_MODELS:
        cost = "zerodha"
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        capital = DEFAULT_CAPITAL
    if not math.isfinite(capital) or capital <= 0:
        capital = DEFAULT_CAPITAL

    try:
        if source == "db":
            source_obj, n_bars = _build_db_source(symbol, exchange, interval, start, end)
        else:
            source = "demo"
            source_obj = _build_synthetic_source(symbol, exchange, n_bars)
        result = _run_engine(source_obj, symbol, capital, cost, window)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception:
        logger.exception("backtest failed for %s/%s source=%s", symbol, exchange, source)
        return {"status": "error", "message": "backtest engine failed — see logs"}

    curve = result.equity_curve
    equity_curve = [[ts.isoformat(), _json_safe(eq)] for ts, eq in curve]
    equity = [{"date": ts.isoformat(), "value": _json_safe(eq)} for ts, eq in curve]

    return {
        "status": "success",
        "strategy": "DemoMomentum",
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "source": source,
        "capital": capital,
        "cost_model": cost,
        "n_bars": n_bars,
        "metrics": _json_safe(getattr(result, "metrics", {})),
        "equity": equity,            # [{date, value}] — for the React chart
        "equity_curve": equity_curve,  # [[iso, value]] — for the server-side SVG
        "trades": _json_safe(getattr(result, "trades", [])),
    }


def run_demo_backtest(symbol: str = DEFAULT_SYMBOL, capital: float = DEFAULT_CAPITAL,
                      cost: str = "zerodha", n_bars: int = 120, window: int = 8) -> dict[str, Any]:
    """Backwards-compatible synthetic demo (delegates to run_backtest)."""
    return run_backtest(source="demo", symbol=symbol, capital=capital, cost=cost,
                        n_bars=n_bars, window=window)
