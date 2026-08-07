"""Monte Carlo simulation on backtest trade results.

Reshuffles the sequence of trade PnLs N times to generate a distribution of
possible equity curves. This tells you: "given these trade results in a
different order, what's the range of outcomes?" — separating luck from edge.

Key outputs:
- Median, 5th, 95th percentile of final equity
- Distribution of max drawdowns
- Probability of ruin (equity dropping below X% of starting capital)
- Confidence interval on the strategy's edge
"""

from __future__ import annotations

import random
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)


def run_monte_carlo(
    trade_pnls: list[float],
    starting_capital: float = 1_000_000.0,
    n_simulations: int = 1000,
    ruin_threshold_pct: float = 0.50,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run Monte Carlo simulation by reshuffling trade PnLs.

    Args:
        trade_pnls: list of per-trade PnL in INR (positive = win, negative = loss)
        starting_capital: starting equity
        n_simulations: number of random reshuffles
        ruin_threshold_pct: equity drop below this % of starting = "ruin"
        seed: random seed for reproducibility

    Returns:
        dict with simulation results, equity percentile curves, and risk metrics.
    """
    if not trade_pnls:
        return {"status": "error", "message": "no trades to simulate"}

    rng = random.Random(seed or 42)
    n_trades = len(trade_pnls)
    ruin_level = starting_capital * ruin_threshold_pct

    final_equities: list[float] = []
    max_drawdowns: list[float] = []
    ruin_count = 0

    # Store percentile curves (5th, 25th, 50th, 75th, 95th)
    all_curves: list[list[float]] = []

    for _ in range(n_simulations):
        shuffled = trade_pnls[:]
        rng.shuffle(shuffled)

        equity = starting_capital
        peak = equity
        max_dd = 0.0
        curve = [equity]
        hit_ruin = False

        for pnl in shuffled:
            equity += pnl
            curve.append(equity)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
            if equity < ruin_level:
                hit_ruin = True

        final_equities.append(equity)
        max_drawdowns.append(max_dd * 100)
        if hit_ruin:
            ruin_count += 1
        all_curves.append(curve)

    final_equities.sort()
    max_drawdowns.sort()

    def percentile(arr: list[float], p: float) -> float:
        idx = int(len(arr) * p / 100)
        return arr[min(idx, len(arr) - 1)]

    # Build percentile equity curves for charting
    curve_len = n_trades + 1
    pct_curves = {}
    for pct_label, pct_val in [("p5", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95)]:
        pct_curve = []
        for step in range(curve_len):
            values = sorted([c[step] if step < len(c) else c[-1] for c in all_curves])
            pct_curve.append(round(percentile(values, pct_val), 2))
        pct_curves[pct_label] = pct_curve

    avg_pnl = sum(trade_pnls) / n_trades
    win_trades = [p for p in trade_pnls if p > 0]
    loss_trades = [p for p in trade_pnls if p <= 0]
    avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
    avg_loss = sum(loss_trades) / len(loss_trades) if loss_trades else 0

    return {
        "status": "success",
        "n_trades": n_trades,
        "n_simulations": n_simulations,
        "starting_capital": starting_capital,
        "trade_stats": {
            "total_pnl": round(sum(trade_pnls), 2),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "win_rate": round(len(win_trades) / n_trades * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(sum(win_trades) / sum(loss_trades)), 2) if loss_trades and sum(loss_trades) != 0 else None,
        },
        "simulation_results": {
            "final_equity_median": round(percentile(final_equities, 50), 0),
            "final_equity_p5": round(percentile(final_equities, 5), 0),
            "final_equity_p95": round(percentile(final_equities, 95), 0),
            "final_equity_worst": round(final_equities[0], 0),
            "final_equity_best": round(final_equities[-1], 0),
            "max_drawdown_median": round(percentile(max_drawdowns, 50), 2),
            "max_drawdown_p95": round(percentile(max_drawdowns, 95), 2),
            "max_drawdown_worst": round(max_drawdowns[-1], 2),
            "probability_of_ruin": round(ruin_count / n_simulations * 100, 2),
            "probability_of_profit": round(sum(1 for e in final_equities if e > starting_capital) / n_simulations * 100, 1),
        },
        "percentile_curves": pct_curves,
    }
