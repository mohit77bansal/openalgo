"""Backtest and live engine.

Same engine drives backtest, paper, and live — only the data source and order
sink differ. This eliminates simulation-to-live drift, the single biggest
source of trader pain.

Public API:
    Strategy           - base class for user strategies
    Context            - injected runtime API (positions, orders, time, data)
    BacktestEngine     - drives a backtest from historical bars/ticks
    Result             - backtest output (PnL, trades, metrics)
"""

from qbacktest.engine.context import Context
from qbacktest.engine.strategy import Strategy
from qbacktest.engine.engine import BacktestEngine
from qbacktest.engine.expectancy import ExpectancyMetrics, compute_expectancy
from qbacktest.engine.result import Result, equity_curve_metrics

__all__ = [
    "BacktestEngine",
    "Context",
    "ExpectancyMetrics",
    "Result",
    "Strategy",
    "compute_expectancy",
    "equity_curve_metrics",
]
