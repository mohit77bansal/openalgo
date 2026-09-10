"""Backtest engine — run_backtest / run_demo_backtest / run_multi_instrument_backtest.

Extracted from backtest_service.py.  This module is the public API for
backtest execution.  It delegates to:
- ``backtest_instruments`` for data source construction,
- ``backtest_metrics`` for JSON sanitisation and Sharpe / drawdown,
- ``services.strategies`` for the strategy registry + factory.

All qbacktest imports are lazy (inside functions) to match the existing
pattern and avoid import-time side effects.
"""

from __future__ import annotations

import math
from typing import Any

from utils.logging import get_logger

from services.backtest_instruments import (
    _build_db_source,
    _build_synthetic_source,
    _instrument_for,
    _StaticSource,
)
from services.backtest_metrics import (
    _compute_max_drawdown,
    _compute_sharpe,
    _json_safe,
)

logger = get_logger(__name__)

# Re-import constants that callers may rely on.
DEMO_COST_MODELS = ("zerodha", "angelone", "upstox", "fyers", "zero")
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_SYMBOL = "NIFTY"


# --------------------------------------------------------------------------- #
# Strategy helpers (imported from the parallel strategies module)
# --------------------------------------------------------------------------- #
def _get_strategy_registry() -> dict[str, dict[str, Any]]:
    """Lazy accessor — avoids circular imports at module-load time."""
    from services.strategies import STRATEGY_REGISTRY  # type: ignore[import-untyped]
    return STRATEGY_REGISTRY


def _make_strategy(symbol: str, strategy_key: str = "atr_channel_breakout", **kwargs) -> Any:
    """Build a strategy instance from the registry."""
    from services.strategies import _make_strategy as _mk  # type: ignore[import-untyped]
    return _mk(symbol, strategy_key, **kwargs)


# --------------------------------------------------------------------------- #
# Engine invocation (the one seam to offload to a worker in production)
# --------------------------------------------------------------------------- #
def _run_engine(
    source_obj: Any,
    symbol: str,
    capital: float,
    cost: str,
    strategy_key: str = "atr_channel_breakout",
    **strategy_kwargs,
) -> Any:
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.engine.engine import BacktestEngine

    cost_key = "zerodha" if cost == "angelone" else cost
    engine = BacktestEngine(
        strategy=_make_strategy(symbol, strategy_key, **strategy_kwargs),
        data=source_obj,
        starting_capital=capital,
        cost_model=DEFAULT_COST_MODELS[cost_key],
    )
    return engine.run()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_backtest(
    source: str = "demo",
    symbol: str = DEFAULT_SYMBOL,
    exchange: str = "NSE",
    interval: str = "D",
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    strategy_key: str = "sma_momentum",
    n_bars: int = 120,
    _save: bool = True,
    **strategy_kwargs,
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

    registry = _get_strategy_registry()
    strat_entry = registry.get(strategy_key, registry.get("atr_channel_breakout", {}))
    strat_name = strat_entry.get("name", strategy_key)
    strat_desc = strat_entry.get("description", "")
    strat_source = strat_entry.get("source", "")

    try:
        if source == "db":
            source_obj, n_bars = _build_db_source(symbol, exchange, interval, start, end)
        else:
            source = "demo"
            source_obj = _build_synthetic_source(symbol, exchange, n_bars)
        result = _run_engine(source_obj, symbol, capital, cost, strategy_key, **strategy_kwargs)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception:
        logger.exception("backtest failed for %s/%s source=%s", symbol, exchange, source)
        return {"status": "error", "message": "backtest engine failed — see logs"}

    curve = result.equity_curve
    equity_curve = [[ts.isoformat(), _json_safe(eq)] for ts, eq in curve]
    equity = [{"date": ts.isoformat(), "value": _json_safe(eq)} for ts, eq in curve]

    # Compute XIRR (annualized IRR from the equity curve cash flows)
    xirr_pct = None
    if len(curve) >= 2:
        try:
            from scipy.optimize import brentq  # noqa: F401 — kept for parity

            first_ts, first_eq = curve[0]
            last_ts, last_eq = curve[-1]
            days = (last_ts - first_ts).total_seconds() / 86400
            if days > 0 and first_eq > 0:
                # Simple annualized return (CAGR) — more robust than iterative
                # XIRR for a single investment period.
                total_return = (last_eq / first_eq) - 1.0
                years = days / 365.25
                if years > 0 and total_return > -1:
                    xirr_pct = ((1 + total_return) ** (1 / years) - 1) * 100
                elif years > 0:
                    xirr_pct = -100.0  # lost more than 100%
        except Exception:
            pass

    # Compute margin used (realistic SPAN margin for the instrument)
    metrics = _json_safe(getattr(result, "metrics", {}))
    margin_per_lot = 0.0
    margin_used = 0.0
    return_on_margin_pct = None
    inst = _instrument_for(symbol, exchange)
    lot_size = max(getattr(inst, "lot_size", 1) or 1, 1)
    if lot_size > 1 and source_obj and source_obj.bars:  # derivative with real data
        # Get a representative price from the actual bar data
        mid_idx = len(source_obj.bars) // 2
        rep_price = source_obj.bars[mid_idx].close if source_obj.bars else 24500
        margin_pct = 0.12  # NRML (conservative SPAN margin)
        notional_per_lot = rep_price * lot_size
        margin_per_lot = notional_per_lot * margin_pct
        margin_used = margin_per_lot
        net_pnl = (metrics.get("net_pnl") or 0) if isinstance(metrics, dict) else 0
        if margin_used > 0 and net_pnl != 0:
            return_on_margin_pct = (net_pnl / margin_used) * 100

    # Raw OHLC bars for the candlestick chart on the frontend.
    ohlc = [
        {"time": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        for b in source_obj.bars
    ]

    out: dict[str, Any] = {
        "status": "success",
        "strategy": strat_name,
        "strategy_description": strat_desc,
        "strategy_source": strat_source,
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "source": source,
        "start": start,
        "end": end,
        "capital": capital,
        "cost_model": cost,
        "n_bars": n_bars,
        "metrics": metrics,
        "xirr_pct": round(xirr_pct, 2) if xirr_pct is not None else None,
        "margin_used": round(margin_used, 0) if margin_used else None,
        "return_on_margin_pct": round(return_on_margin_pct, 2) if return_on_margin_pct is not None else None,
        "equity": equity,
        "equity_curve": equity_curve,
        "trades": _json_safe(getattr(result, "trades", [])),
        "ohlc": ohlc,
        "data_note": (
            "Continuous NIFTY futures (stitched across contract rolls by broker API)"
            if "FUT" in symbol.upper()
            else None
        ),
    }

    if _save:
        try:
            from database.backtest_db import save_backtest_run

            run_id = save_backtest_run(out)
            if run_id:
                out["run_id"] = run_id
        except Exception:
            logger.debug("failed to persist backtest run", exc_info=True)

    return out


def run_demo_backtest(
    symbol: str = DEFAULT_SYMBOL,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    n_bars: int = 120,
    window: int = 8,
) -> dict[str, Any]:
    """Backwards-compatible synthetic demo (delegates to run_backtest)."""
    return run_backtest(
        source="demo", symbol=symbol, capital=capital, cost=cost,
        n_bars=n_bars, window=window,
    )


def run_multi_instrument_backtest(
    symbols: list[str],
    exchange: str = "NFO",
    interval: str = "15m",
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    strategy_key: str = "atr_channel_breakout",
    source: str = "db",
    weights: list[float] | None = None,
) -> dict[str, Any]:
    """Run one strategy across multiple instruments. Capital split by weights."""
    if not symbols:
        return {"status": "error", "message": "no symbols provided"}
    n = len(symbols)
    if weights is None:
        weights = [1.0 / n] * n

    per_instrument: list[dict[str, Any]] = []
    total_pnl = total_fees = 0.0
    total_trades = 0
    combined_equity: list[dict[str, Any]] = []

    for i, sym in enumerate(symbols):
        alloc = capital * weights[i]
        result = run_backtest(
            source=source, symbol=sym, exchange=exchange, interval=interval,
            start=start, end=end, capital=alloc, cost=cost,
            strategy_key=strategy_key, _save=False,
        )
        m = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
        pnl = m.get("net_pnl", 0) or 0
        fees = m.get("fees_total", 0) or 0
        trades = m.get("n_trades", 0) or 0
        per_instrument.append({
            "symbol": sym,
            "exchange": exchange,
            "capital_allocated": alloc,
            "weight": weights[i],
            "status": result.get("status", "error"),
            "n_bars": result.get("n_bars", 0),
            "n_trades": trades,
            "net_pnl": pnl,
            "pnl_pct": (pnl / alloc * 100) if alloc else 0,
            "fees_total": fees,
            "metrics": m,
            "equity": result.get("equity", []),
            "trades": result.get("trades", []),
            "ohlc": result.get("ohlc", []),
            "sharpe": _compute_sharpe(result.get("equity", [])),
            "max_drawdown_pct": _compute_max_drawdown(result.get("equity", [])),
            "run_id": result.get("run_id"),
        })
        total_pnl += pnl
        total_fees += fees
        total_trades += trades

    # Build combined (portfolio-level) equity curve.
    if per_instrument:
        max_len = max(len(r["equity"]) for r in per_instrument)
        for step in range(max_len):
            total_val = 0.0
            date_str = ""
            for r in per_instrument:
                eq = r["equity"]
                if step < len(eq):
                    total_val += eq[step].get("value", 0)
                    date_str = eq[step].get("date", date_str)
                elif eq:
                    total_val += eq[-1].get("value", 0)
            combined_equity.append({"date": date_str, "value": total_val})

    registry = _get_strategy_registry()
    strat_entry = registry.get(strategy_key, {})
    out: dict[str, Any] = {
        "status": "success",
        "strategy": strat_entry.get("name", strategy_key),
        "strategy_description": strat_entry.get("description", ""),
        "strategy_source": strat_entry.get("source", ""),
        "symbol": " + ".join(symbols),
        "symbols": symbols,
        "exchange": exchange,
        "interval": interval,
        "source": source,
        "start": start,
        "end": end,
        "capital": capital,
        "total_capital": capital,
        "cost_model": cost,
        "n_instruments": n,
        "n_bars": sum(p.get("n_bars", 0) for p in per_instrument),
        "metrics": {
            "net_pnl": round(total_pnl, 2),
            "fees_total": round(total_fees, 2),
            "n_trades": total_trades,
            "sharpe": _compute_sharpe(combined_equity),
            "max_drawdown_pct": _compute_max_drawdown(combined_equity),
        },
        "portfolio_metrics": {
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / capital * 100, 2) if capital else 0,
            "total_fees": round(total_fees, 2),
            "total_trades": total_trades,
        },
        "equity": combined_equity,
        "per_instrument": [
            {k: v for k, v in p.items() if k not in ("equity", "trades", "ohlc")}
            for p in per_instrument
        ],
        "combined_equity": combined_equity,
        "trades": [t for p in per_instrument for t in (p.get("trades") or [])],
        "ohlc": [b for p in per_instrument[:1] for b in (p.get("ohlc") or [])],
    }

    # Compute portfolio-level XIRR and margin for storage.
    if combined_equity and len(combined_equity) >= 2:
        try:
            first_val = combined_equity[0].get("value", capital)
            last_val = combined_equity[-1].get("value", capital)
            from datetime import datetime as _dt

            first_d = _dt.fromisoformat(combined_equity[0].get("date", "2026-01-01"))
            last_d = _dt.fromisoformat(combined_equity[-1].get("date", "2026-08-07"))
            days = (last_d - first_d).total_seconds() / 86400
            if days > 0 and first_val > 0:
                total_return = (last_val / first_val) - 1.0
                years = days / 365.25
                if years > 0 and total_return > -1:
                    out["xirr_pct"] = round(((1 + total_return) ** (1 / years) - 1) * 100, 2)
                elif years > 0:
                    out["xirr_pct"] = -100.0
        except Exception:
            pass

    # Margin: sum of per-instrument margins.
    total_margin = 0.0
    for p in per_instrument:
        alloc = p.get("capital_allocated", 0)
        if alloc > 0:
            total_margin += alloc * 0.12  # 12% SPAN margin
    if total_margin > 0:
        out["margin_used"] = round(total_margin, 0)
        if total_pnl != 0:
            out["return_on_margin_pct"] = round((total_pnl / total_margin) * 100, 2)

    # Save one portfolio-level row to the DB (not per-instrument).
    try:
        from database.backtest_db import save_backtest_run

        run_id = save_backtest_run(out)
        if run_id:
            out["run_id"] = run_id
    except Exception:
        logger.debug("failed to persist multi-instrument backtest", exc_info=True)

    return out
