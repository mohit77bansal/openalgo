"""Backtest metrics — JSON sanitisation and portfolio-level computations.

Extracted from backtest_service.py to keep the monolith focused on
orchestration.  All functions are pure (no side effects, no mutation).
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# JSON sanitisation
# --------------------------------------------------------------------------- #
def _json_safe(obj: Any) -> Any:
    """Recursively convert *obj* to a JSON-serialisable value.

    Handles float edge cases (NaN / Inf -> None), datetimes, enums,
    frozen dataclasses, and nested dicts / lists.
    """
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
# Portfolio-level metrics
# --------------------------------------------------------------------------- #
def _compute_sharpe(equity: list[dict[str, Any]], risk_free_rate: float = 0.07) -> float | None:
    """Compute annualized Sharpe ratio from equity curve ``[{date, value}]``.

    Assumes ~25 intraday bars per trading day (15-min data) and 252 trading
    days per year.  Returns ``None`` when the sample is too small,
    volatility is zero, or equity goes non-positive (margin-call scenario).
    """
    if not equity or len(equity) < 10:
        return None
    values = [pt.get("value", 0) for pt in equity if pt.get("value", 0) > 0]
    if len(values) < 10:
        return None
    returns = [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values))
        if values[i - 1] > 0
    ]
    if len(returns) < 5:
        return None
    avg_ret = sum(returns) / len(returns)
    std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
    if std_ret == 0:
        return None
    periods_per_year = 252 * 25
    annual_ret = avg_ret * periods_per_year
    annual_std = std_ret * (periods_per_year ** 0.5)
    sharpe = round((annual_ret - risk_free_rate) / annual_std, 2)
    total_return = (values[-1] - values[0]) / values[0] if values[0] > 0 else -1
    if total_return < 0 and sharpe > 0:
        return round(-abs(sharpe), 2)
    return sharpe


def _compute_max_drawdown(equity: list[dict[str, Any]]) -> float | None:
    """Compute max drawdown % from an equity curve ``[{date, value}]``.

    Returns a *negative* percentage capped at -100% (e.g. ``-12.34``).
    In F&O with leverage, equity can go below zero, but drawdown beyond
    -100% is meaningless -- you'd be margin-called.  Returns ``None``
    if the curve has fewer than two points.
    """
    if not equity or len(equity) < 2:
        return None
    peak = 0.0
    max_dd = 0.0
    for pt in equity:
        val = pt.get("value", 0)
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
    dd_pct = round(-min(max_dd, 1.0) * 100, 2)
    return dd_pct if max_dd > 0 else 0.0
