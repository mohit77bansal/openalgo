"""Walk-forward optimization.

Splits a date range into rolling (train, test) windows. Optimizer picks the best
parameter set on the train window; evaluator measures it on the held-out test
window. Roll forward and repeat.

This is the standard defense against curve-fitting. Reports OOS Sharpe
degradation — the gap between in-sample and out-of-sample performance is the
honest measure of strategy robustness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from qbacktest.optimizer.grid import ParamSpace, grid_search


@dataclass
class WalkForwardSplitter:
    """Generates (train_start, train_end, test_start, test_end) tuples."""

    start: date
    end: date
    train_days: int
    test_days: int
    step_days: int = 0       # 0 means non-overlapping, equal to test_days

    def __post_init__(self) -> None:
        if self.step_days <= 0:
            self.step_days = self.test_days

    def windows(self) -> list[tuple[date, date, date, date]]:
        out = []
        cursor = self.start
        while True:
            train_end = cursor + timedelta(days=self.train_days - 1)
            test_start = train_end + timedelta(days=1)
            test_end = test_start + timedelta(days=self.test_days - 1)
            if test_end > self.end:
                break
            out.append((cursor, train_end, test_start, test_end))
            cursor += timedelta(days=self.step_days)
        return out


@dataclass
class WalkForwardResult:
    """Result of a walk-forward run."""
    windows: pd.DataFrame                    # per-window: train/test ranges + metrics
    best_params_by_window: list[dict[str, Any]]
    in_sample_sharpe_avg: float
    out_of_sample_sharpe_avg: float
    degradation: float                       # in_sample - out_of_sample (lower better)


def walk_forward(
    splitter: WalkForwardSplitter,
    space: ParamSpace,
    train_run_fn: Callable[[dict[str, Any], date, date], dict[str, float]],
    test_run_fn: Callable[[dict[str, Any], date, date], dict[str, float]],
    *,
    parallel: int = 1,
) -> WalkForwardResult:
    """Run walk-forward optimization.

    Parameters
    ----------
    splitter
        Generates windows.
    space
        Parameter space to search on each train window.
    train_run_fn(params, start, end) -> metrics
        Backtest on the train window.
    test_run_fn(params, start, end) -> metrics
        Backtest on the test window.
    """
    rows = []
    best_per_window = []
    is_sharpes = []
    oos_sharpes = []
    for tr_s, tr_e, te_s, te_e in splitter.windows():
        # Optimize on train
        df = grid_search(
            lambda p: train_run_fn(p, tr_s, tr_e),
            space,
            parallel=parallel,
            progress=False,
        )
        if df.empty:
            continue
        best = df.iloc[0]
        best_params = {k: best[k] for k in space.grid.keys()}
        # Evaluate on test
        oos = test_run_fn(best_params, te_s, te_e)
        rows.append({
            "train_start": tr_s,
            "train_end": tr_e,
            "test_start": te_s,
            "test_end": te_e,
            "in_sample_sharpe": float(best.get("sharpe", 0)),
            "out_of_sample_sharpe": float(oos.get("sharpe", 0)),
            "in_sample_return": float(best.get("total_return", 0)),
            "out_of_sample_return": float(oos.get("total_return", 0)),
            **best_params,
        })
        best_per_window.append(best_params)
        is_sharpes.append(float(best.get("sharpe", 0)))
        oos_sharpes.append(float(oos.get("sharpe", 0)))
    df = pd.DataFrame(rows)
    is_avg = sum(is_sharpes) / max(len(is_sharpes), 1)
    oos_avg = sum(oos_sharpes) / max(len(oos_sharpes), 1)
    return WalkForwardResult(
        windows=df,
        best_params_by_window=best_per_window,
        in_sample_sharpe_avg=is_avg,
        out_of_sample_sharpe_avg=oos_avg,
        degradation=is_avg - oos_avg,
    )
