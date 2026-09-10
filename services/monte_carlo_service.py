"""Monte Carlo simulation -- hedge-fund-grade methodology.

Three simulation methods:
1. **Reshuffle** (standard) -- random permutation of trade PnLs
2. **Block bootstrap** -- shuffles blocks of consecutive trades,
   preserving win/loss streaks and serial correlation
3. **Parametric** -- samples from fitted distribution (normal or
   student-t for fat tails), capturing tail risk better

Additional risk metrics beyond basic P&L distribution:
- Conditional Value at Risk (CVaR / Expected Shortfall)
- Maximum underwater period (days in drawdown)
- Calmar ratio (annualized return / max drawdown)
- Kelly fraction (optimal bet size)
- Edge significance (z-score: is the edge real or luck?)
- Sortino ratio (downside-deviation-adjusted return)
"""

from __future__ import annotations

import math
import random
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)


def _percentile(arr: list[float], p: float) -> float:
    if not arr:
        return 0.0
    idx = int(len(arr) * p / 100)
    return arr[min(idx, len(arr) - 1)]


def _block_bootstrap(
    trade_pnls: list[float], block_size: int, rng: random.Random
) -> list[float]:
    """Sample blocks of consecutive trades with replacement."""
    n = len(trade_pnls)
    if block_size >= n:
        result = trade_pnls[:]
        rng.shuffle(result)
        return result
    result: list[float] = []
    while len(result) < n:
        start = rng.randint(0, n - block_size)
        result.extend(trade_pnls[start : start + block_size])
    return result[:n]


def _parametric_sample(
    trade_pnls: list[float], rng: random.Random
) -> list[float]:
    """Sample from fitted distribution (normal with fat-tail adjustment)."""
    n = len(trade_pnls)
    mean = sum(trade_pnls) / n
    var = sum((p - mean) ** 2 for p in trade_pnls) / n
    std = var**0.5 if var > 0 else 1.0
    kurtosis = sum((p - mean) ** 4 for p in trade_pnls) / (n * var**2) - 3 if var > 0 else 0
    # Student-t degrees of freedom from excess kurtosis (fat tails)
    # kurtosis = 6/(df-4) for t-distribution, so df = 6/kurtosis + 4
    if kurtosis > 0.5:
        df = max(6 / kurtosis + 4, 5)
        # Approximate t-distribution sampling via normal * chi-squared ratio
        result = []
        for _ in range(n):
            z = rng.gauss(0, 1)
            chi2 = sum(rng.gauss(0, 1) ** 2 for _ in range(int(df))) / df
            t = z / max(chi2**0.5, 0.01)
            result.append(mean + std * t * ((df - 2) / df) ** 0.5)
        return result
    return [rng.gauss(mean, std) for _ in range(n)]


def _simulate_path(
    pnls: list[float], starting_capital: float, ruin_level: float
) -> dict[str, Any]:
    """Run one equity path and compute all metrics."""
    equity = starting_capital
    peak = equity
    max_dd = 0.0
    curve = [equity]
    hit_ruin = False
    underwater_bars = 0
    max_underwater = 0
    current_underwater = 0

    for pnl in pnls:
        equity += pnl
        curve.append(max(equity, 0))

        if equity > peak:
            peak = equity
            current_underwater = 0
        else:
            current_underwater += 1
            if current_underwater > max_underwater:
                max_underwater = current_underwater

        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        if equity < ruin_level:
            hit_ruin = True

    return {
        "final_equity": equity,
        "max_drawdown_pct": max_dd * 100,
        "max_underwater_bars": max_underwater,
        "hit_ruin": hit_ruin,
        "curve": curve,
    }


def run_monte_carlo(
    trade_pnls: list[float],
    starting_capital: float = 1_000_000.0,
    n_simulations: int = 1000,
    ruin_threshold_pct: float = 0.50,
    seed: int | None = None,
    method: str = "block",
    block_size: int | None = None,
) -> dict[str, Any]:
    """Run Monte Carlo simulation with hedge-fund-grade methodology.

    Methods:
        "reshuffle": random permutation (classic)
        "block": block bootstrap preserving streaks (default)
        "parametric": sample from fitted distribution (fat tails)

    Returns comprehensive risk metrics including CVaR, Kelly, edge significance.
    """
    if not trade_pnls:
        return {"status": "error", "message": "no trades to simulate"}

    rng = random.Random(seed or 42)
    n_trades = len(trade_pnls)
    ruin_level = starting_capital * ruin_threshold_pct

    if block_size is None:
        block_size = max(3, int(n_trades**0.5))

    final_equities: list[float] = []
    max_drawdowns: list[float] = []
    max_underwaters: list[int] = []
    ruin_count = 0
    all_curves: list[list[float]] = []

    for _ in range(n_simulations):
        if method == "parametric":
            shuffled = _parametric_sample(trade_pnls, rng)
        elif method == "block":
            shuffled = _block_bootstrap(trade_pnls, block_size, rng)
        else:
            shuffled = trade_pnls[:]
            rng.shuffle(shuffled)

        path = _simulate_path(shuffled, starting_capital, ruin_level)
        final_equities.append(path["final_equity"])
        max_drawdowns.append(path["max_drawdown_pct"])
        max_underwaters.append(path["max_underwater_bars"])
        if path["hit_ruin"]:
            ruin_count += 1
        all_curves.append(path["curve"])

    final_equities.sort()
    max_drawdowns.sort()
    max_underwaters.sort()

    # Percentile equity curves for charting
    curve_len = n_trades + 1
    pct_curves: dict[str, list[float]] = {}
    for label, pval in [("p5", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95)]:
        pct_curve = []
        for step in range(curve_len):
            values = sorted(c[step] if step < len(c) else c[-1] for c in all_curves)
            pct_curve.append(round(_percentile(values, pval), 2))
        pct_curves[label] = pct_curve

    # Trade statistics
    win_trades = [p for p in trade_pnls if p > 0]
    loss_trades = [p for p in trade_pnls if p <= 0]
    avg_pnl = sum(trade_pnls) / n_trades
    avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
    avg_loss = sum(loss_trades) / len(loss_trades) if loss_trades else 0
    win_rate = len(win_trades) / n_trades
    gross_win = sum(win_trades)
    gross_loss = abs(sum(loss_trades))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # CVaR (Expected Shortfall) -- average of worst 5% of final equities
    worst_5pct_count = max(1, int(n_simulations * 0.05))
    cvar_equity = sum(final_equities[:worst_5pct_count]) / worst_5pct_count
    cvar_loss = starting_capital - cvar_equity

    # Edge significance (z-score)
    # H0: avg_pnl = 0. z = avg_pnl / (std / sqrt(n))
    pnl_std = (sum((p - avg_pnl) ** 2 for p in trade_pnls) / n_trades) ** 0.5
    z_score = (avg_pnl / (pnl_std / n_trades**0.5)) if pnl_std > 0 else 0
    edge_significant = abs(z_score) > 1.96

    # Kelly fraction (optimal bet size)
    # Kelly = (win_rate * avg_win/avg_loss - (1 - win_rate)) / (avg_win/avg_loss)
    if avg_loss != 0 and avg_win > 0:
        b = abs(avg_win / avg_loss)
        kelly = (win_rate * b - (1 - win_rate)) / b
        kelly = max(0, min(kelly, 1))
    else:
        kelly = 0

    # Sortino ratio (uses downside deviation instead of total std)
    downside_returns = [p for p in trade_pnls if p < 0]
    if downside_returns:
        downside_dev = (sum(p**2 for p in downside_returns) / len(downside_returns)) ** 0.5
        sortino = (avg_pnl * 252) / (downside_dev * 252**0.5) if downside_dev > 0 else 0
    else:
        sortino = float("inf") if avg_pnl > 0 else 0

    # Calmar ratio (annualized return / max drawdown)
    total_return = sum(trade_pnls)
    median_dd = _percentile(max_drawdowns, 50)
    calmar = abs(total_return / starting_capital * 100 / median_dd) if median_dd > 0 else 0

    return {
        "status": "success",
        "n_trades": n_trades,
        "n_simulations": n_simulations,
        "starting_capital": starting_capital,
        "method": method,
        "block_size": block_size if method == "block" else None,
        "trade_stats": {
            "total_pnl": round(sum(trade_pnls), 2),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "win_rate": round(win_rate * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": profit_factor,
            "kelly_fraction": round(kelly, 3),
            "sortino_ratio": round(sortino, 2) if sortino != float("inf") else None,
            "edge_z_score": round(z_score, 2),
            "edge_significant": edge_significant,
        },
        "simulation_results": {
            "final_equity_median": round(_percentile(final_equities, 50), 0),
            "final_equity_p5": round(_percentile(final_equities, 5), 0),
            "final_equity_p95": round(_percentile(final_equities, 95), 0),
            "final_equity_worst": round(final_equities[0], 0),
            "final_equity_best": round(final_equities[-1], 0),
            "max_drawdown_median": round(_percentile(max_drawdowns, 50), 2),
            "max_drawdown_p95": round(_percentile(max_drawdowns, 95), 2),
            "max_drawdown_worst": round(max_drawdowns[-1], 2),
            "max_underwater_median": int(_percentile([float(u) for u in max_underwaters], 50)),
            "max_underwater_p95": int(_percentile([float(u) for u in max_underwaters], 95)),
            "probability_of_ruin": round(ruin_count / n_simulations * 100, 2),
            "probability_of_profit": round(
                sum(1 for e in final_equities if e > starting_capital) / n_simulations * 100, 1
            ),
            "cvar_5pct": round(cvar_loss, 0),
            "calmar_ratio": round(calmar, 2),
        },
        "percentile_curves": pct_curves,
    }
