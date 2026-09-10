"""Tests for services/backtest_metrics.py — JSON sanitisation and metric computation."""

import math
import sys
import os
from datetime import date, datetime
from enum import Enum

# Ensure the project root is on sys.path so `services.*` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.backtest_metrics import _json_safe, _compute_sharpe, _compute_max_drawdown


# ──────────────────────────────────────────────────────────────
# _json_safe
# ──────────────────────────────────────────────────────────────

class TestJsonSafe:
    def test_none(self):
        assert _json_safe(None) is None

    def test_bool(self):
        assert _json_safe(True) is True
        assert _json_safe(False) is False

    def test_int(self):
        assert _json_safe(42) == 42

    def test_str(self):
        assert _json_safe("hello") == "hello"

    def test_finite_float(self):
        assert _json_safe(3.14) == 3.14

    def test_nan_becomes_none(self):
        assert _json_safe(float("nan")) is None

    def test_inf_becomes_none(self):
        assert _json_safe(float("inf")) is None
        assert _json_safe(float("-inf")) is None

    def test_date(self):
        assert _json_safe(date(2026, 8, 9)) == "2026-08-09"

    def test_datetime(self):
        result = _json_safe(datetime(2026, 8, 9, 14, 30, 0))
        assert result.startswith("2026-08-09")

    def test_enum(self):
        class Color(Enum):
            RED = "red"
        assert _json_safe(Color.RED) == "red"

    def test_dict(self):
        assert _json_safe({"a": 1, "b": float("nan")}) == {"a": 1, "b": None}

    def test_list(self):
        assert _json_safe([1, float("inf"), "x"]) == [1, None, "x"]

    def test_nested(self):
        result = _json_safe({"items": [1, {"v": float("nan")}]})
        assert result == {"items": [1, {"v": None}]}

    def test_unknown_type_becomes_str(self):
        result = _json_safe(object)
        assert isinstance(result, str)


# ──────────────────────────────────────────────────────────────
# _compute_sharpe
# ──────────────────────────────────────────────────────────────

class TestComputeSharpe:
    def test_empty_returns_none(self):
        assert _compute_sharpe([]) is None

    def test_too_few_points_returns_none(self):
        assert _compute_sharpe([{"date": "2026-01-01", "value": 100}] * 5) is None

    def test_constant_equity_returns_none(self):
        """Zero volatility should return None (division by zero guard)."""
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000} for i in range(20)]
        assert _compute_sharpe(curve) is None

    def test_rising_equity_positive_sharpe(self):
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000 + i * 5000} for i in range(30)]
        sharpe = _compute_sharpe(curve)
        assert sharpe is not None
        assert sharpe > 0

    def test_falling_equity_negative_sharpe(self):
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000 - i * 5000} for i in range(30)]
        sharpe = _compute_sharpe(curve)
        assert sharpe is not None
        assert sharpe < 0

    def test_returns_float_rounded_to_2dp(self):
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000 + i * 1000 + (i % 3) * 500} for i in range(30)]
        sharpe = _compute_sharpe(curve)
        if sharpe is not None:
            assert sharpe == round(sharpe, 2)

    def test_negative_total_return_forces_negative_sharpe(self):
        """Sharpe must be negative when total return is negative, even if per-bar
        returns are noisy enough to make the raw calculation positive."""
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000 - i * 2000 + (i % 5) * 3000} for i in range(30)]
        if curve[-1]["value"] < curve[0]["value"]:
            sharpe = _compute_sharpe(curve)
            if sharpe is not None:
                assert sharpe <= 0, f"Sharpe {sharpe} should be <= 0 for negative total return"

    def test_equity_going_below_zero_returns_none_or_negative(self):
        """When equity goes negative (margin call), Sharpe should be None or negative."""
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000000 - i * 50000} for i in range(30)]
        sharpe = _compute_sharpe(curve)
        assert sharpe is None or sharpe < 0


# ──────────────────────────────────────────────────────────────
# _compute_max_drawdown
# ──────────────────────────────────────────────────────────────

class TestComputeMaxDrawdown:
    def test_empty_returns_none(self):
        assert _compute_max_drawdown([]) is None

    def test_single_point_returns_none(self):
        assert _compute_max_drawdown([{"date": "2026-01-01", "value": 100}]) is None

    def test_monotonically_rising_returns_zero(self):
        curve = [{"date": f"2026-01-{i+1:02d}", "value": 1000 + i * 100} for i in range(10)]
        assert _compute_max_drawdown(curve) == 0.0

    def test_50pct_drawdown(self):
        curve = [
            {"date": "2026-01-01", "value": 1000},
            {"date": "2026-01-02", "value": 500},
            {"date": "2026-01-03", "value": 600},
        ]
        dd = _compute_max_drawdown(curve)
        assert dd is not None
        assert dd == -50.0

    def test_drawdown_is_negative(self):
        curve = [
            {"date": "2026-01-01", "value": 1000},
            {"date": "2026-01-02", "value": 900},
            {"date": "2026-01-03", "value": 1100},
            {"date": "2026-01-04", "value": 800},
        ]
        dd = _compute_max_drawdown(curve)
        assert dd is not None
        assert dd < 0

    def test_worst_drawdown_captured(self):
        """Peak at 1100, trough at 800 = -27.27%."""
        curve = [
            {"date": "2026-01-01", "value": 1000},
            {"date": "2026-01-02", "value": 1100},
            {"date": "2026-01-03", "value": 800},
            {"date": "2026-01-04", "value": 1050},
        ]
        dd = _compute_max_drawdown(curve)
        assert dd is not None
        expected = round(-(300 / 1100) * 100, 2)
        assert dd == expected

    def test_drawdown_capped_at_100pct(self):
        """Equity going negative (margin call) should cap drawdown at -100%."""
        curve = [
            {"date": "2026-01-01", "value": 1000},
            {"date": "2026-01-02", "value": 1200},
            {"date": "2026-01-03", "value": -200},
        ]
        dd = _compute_max_drawdown(curve)
        assert dd is not None
        assert dd == -100.0, f"Expected -100.0 (capped), got {dd}"

    def test_drawdown_never_below_minus_100(self):
        """Even with deeply negative equity, DD stays at -100%."""
        curve = [
            {"date": "2026-01-01", "value": 500000},
            {"date": "2026-01-02", "value": 600000},
            {"date": "2026-01-03", "value": -500000},
        ]
        dd = _compute_max_drawdown(curve)
        assert dd is not None
        assert dd >= -100.0, f"DD {dd} should be >= -100.0"
