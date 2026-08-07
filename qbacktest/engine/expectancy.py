"""Honest expectancy metrics — designed to make loss-tail strategies visible.

Most retail "scalping" strategies hide behind their win rate. A 90%-win-rate
strategy can have negative expectancy if avg_loss is 9× avg_win. This module
computes and surfaces the math that exposes that.

Public API:
    ExpectancyMetrics  - frozen dataclass with the honest math
    compute_expectancy(trades) -> ExpectancyMetrics
    expectancy_table(trades) -> string for CLI/log display
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from qbacktest.core.types import Trade


@dataclass(frozen=True, slots=True)
class ExpectancyMetrics:
    """All the numbers a retail trader should see BEFORE deciding to run a strategy."""

    n_trades: int

    # Win-side
    n_wins: int
    win_rate: float          # 0..1
    avg_win_inr: float
    median_win_inr: float
    best_trade_inr: float

    # Loss-side
    n_losses: int
    loss_rate: float
    avg_loss_inr: float       # negative
    median_loss_inr: float    # negative
    worst_trade_inr: float    # negative

    # Combined
    expectancy_per_trade_inr: float    # E[trade] = wr*avg_win + lr*avg_loss
    expectancy_per_trade_pct_of_avg_win: float   # how many avg-wins is 1 unit of expectancy worth?
    profit_factor: float       # gross_profit / |gross_loss|
    win_loss_ratio: float      # avg_win / |avg_loss|
    payoff_to_breakeven_win_rate: float   # the win rate this R:R would need to break even

    # Loss-tail
    worst_day_loss_inr: float   # largest single-day net loss
    worst_5_pct_avg_loss: float # avg of bottom 5% of trades
    consecutive_losses_max: int
    consecutive_losses_avg: float

    # Quality flags
    is_positive_expectancy: bool
    expectancy_dominated_by_outliers: bool   # true if top 10% of wins drive >50% of total profit
    has_blow_up_risk: bool   # true if worst_trade / avg_win > 5

    # For honesty disclosure
    notes: list[str] = field(default_factory=list, hash=False)

    def to_json(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4),
            "loss_rate": round(self.loss_rate, 4),
            "avg_win_inr": round(self.avg_win_inr, 2),
            "avg_loss_inr": round(self.avg_loss_inr, 2),
            "best_trade_inr": round(self.best_trade_inr, 2),
            "worst_trade_inr": round(self.worst_trade_inr, 2),
            "expectancy_per_trade_inr": round(self.expectancy_per_trade_inr, 2),
            "profit_factor": round(self.profit_factor, 4) if math.isfinite(self.profit_factor) else None,
            "win_loss_ratio": round(self.win_loss_ratio, 4) if math.isfinite(self.win_loss_ratio) else None,
            "payoff_to_breakeven_win_rate": round(self.payoff_to_breakeven_win_rate, 4),
            "worst_day_loss_inr": round(self.worst_day_loss_inr, 2),
            "worst_5_pct_avg_loss": round(self.worst_5_pct_avg_loss, 2),
            "consecutive_losses_max": self.consecutive_losses_max,
            "consecutive_losses_avg": round(self.consecutive_losses_avg, 2),
            "is_positive_expectancy": self.is_positive_expectancy,
            "expectancy_dominated_by_outliers": self.expectancy_dominated_by_outliers,
            "has_blow_up_risk": self.has_blow_up_risk,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """Human-readable summary that doesn't hide the loss tail."""
        if self.n_trades == 0:
            return "No trades — nothing to evaluate."
        lines = [
            f"Trades:                  {self.n_trades}",
            f"Win rate:                {self.win_rate*100:.1f}%  ({self.n_wins}/{self.n_trades})",
            f"Avg win / avg loss:      ₹{self.avg_win_inr:,.0f}  /  ₹{self.avg_loss_inr:,.0f}",
            f"Win/loss ratio (R:R):    {self.win_loss_ratio:.2f}x" if math.isfinite(self.win_loss_ratio) else "Win/loss ratio:          n/a (no losses)",
            f"Profit factor:           {self.profit_factor:.2f}" if math.isfinite(self.profit_factor) else "Profit factor:           n/a (no losses)",
            f"Expectancy per trade:    ₹{self.expectancy_per_trade_inr:,.2f}  ← THE NUMBER THAT MATTERS",
            f"Breakeven win rate:      {self.payoff_to_breakeven_win_rate*100:.1f}%  (you need this win rate to not lose at current R:R)",
            "",
            "Loss tail:",
            f"  Worst single trade:    ₹{self.worst_trade_inr:,.0f}",
            f"  Worst day total:       ₹{self.worst_day_loss_inr:,.0f}",
            f"  Avg of worst 5%:       ₹{self.worst_5_pct_avg_loss:,.0f}",
            f"  Max losing streak:     {self.consecutive_losses_max}",
            "",
            "Flags:",
            f"  {'✓' if self.is_positive_expectancy else '✗'}  Positive expectancy" if self.is_positive_expectancy
                                                                                else f"  ✗  NEGATIVE expectancy — this strategy bleeds long-term",
            f"  {'⚠ Outlier-driven (top 10% of wins → >50% of total profit; one bad regime kills it)' if self.expectancy_dominated_by_outliers else '✓  Profit spread across trades (not outlier-driven)'}",
            f"  {'⚠ BLOW-UP RISK (worst trade > 5× avg win)' if self.has_blow_up_risk else '✓  No catastrophic single-trade risk'}",
        ]
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  · {n}")
        return "\n".join(lines)


def compute_expectancy(trades: Iterable[Trade]) -> ExpectancyMetrics:
    """Compute honest expectancy metrics from a list of round-trip trades.

    Uses `Trade.net_pnl` (already deducts fees) as the per-trade P&L.
    """
    trade_list = list(trades)
    if not trade_list:
        return _empty_metrics()

    pnls = [t.net_pnl for t in trade_list]
    pnls_sorted = sorted(pnls)
    n = len(pnls)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    # Zeros (rare break-even) are counted as neither
    n_wins = len(wins)
    n_losses = len(losses)

    win_rate = n_wins / n if n else 0.0
    loss_rate = n_losses / n if n else 0.0

    avg_win = sum(wins) / n_wins if wins else 0.0
    avg_loss = sum(losses) / n_losses if losses else 0.0    # already negative
    median_win = _median([w for w in wins]) if wins else 0.0
    median_loss = _median([l for l in losses]) if losses else 0.0

    best = max(pnls)
    worst = min(pnls)

    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else math.inf

    # Expectancy
    e = win_rate * avg_win + loss_rate * avg_loss
    # Breakeven win rate given current R:R:
    # 0 = wr * avg_win + (1 - wr) * avg_loss
    # → wr = -avg_loss / (avg_win - avg_loss)   when both are non-zero
    if avg_win > 0 and avg_loss < 0:
        breakeven_wr = -avg_loss / (avg_win - avg_loss)
    else:
        breakeven_wr = 1.0 if avg_loss < 0 else 0.0

    # Worst-day loss: bucket trades by exit date
    by_day: dict[date, float] = defaultdict(float)
    for t in trade_list:
        by_day[t.exit_fill.ts.date()] += t.net_pnl
    worst_day = min(by_day.values()) if by_day else 0.0

    # Worst 5% of trades — at least 1 trade
    bottom_count = max(1, int(round(n * 0.05)))
    worst_5pct = sum(pnls_sorted[:bottom_count]) / bottom_count if pnls_sorted else 0.0

    # Consecutive losing streaks
    longest, current = 0, 0
    all_streaks: list[int] = []
    for p in pnls:
        if p < 0:
            current += 1
            longest = max(longest, current)
        else:
            if current > 0:
                all_streaks.append(current)
            current = 0
    if current > 0:
        all_streaks.append(current)
    avg_streak = sum(all_streaks) / len(all_streaks) if all_streaks else 0.0

    # Flags
    is_pos = e > 0
    # Outlier flag: if top 10% of wins account for >50% of gross profit
    outlier_flag = False
    if wins:
        top_n = max(1, int(round(len(wins) * 0.10)))
        top_wins = sum(sorted(wins, reverse=True)[:top_n])
        outlier_flag = (gross_profit > 0) and (top_wins / gross_profit > 0.50)

    blow_up = False
    if avg_win > 0 and worst < 0:
        blow_up = abs(worst) / avg_win > 5.0

    # Notes — honest disclosures
    notes: list[str] = []
    if not is_pos:
        notes.append("Expectancy is NEGATIVE — running this longer compounds losses.")
    if blow_up:
        notes.append(
            f"Worst trade ₹{worst:,.0f} is {abs(worst) / avg_win:.1f}× the avg win. "
            f"A single bad day can wipe out months of profit."
        )
    if outlier_flag:
        notes.append(
            "Top 10% of wins drive >50% of gross profit. Strategy is outlier-dependent — "
            "miss one of those rare big wins and expectancy turns negative."
        )
    if win_rate > 0.85 and win_loss_ratio < 0.5:
        notes.append(
            "Classic premium-selling fingerprint: high win rate, asymmetric loss. "
            "Survives till the 1-in-N event hits."
        )
    if longest >= 5:
        notes.append(f"Max losing streak was {longest} trades — plan position sizing accordingly.")

    return ExpectancyMetrics(
        n_trades=n,
        n_wins=n_wins,
        win_rate=win_rate,
        avg_win_inr=avg_win,
        median_win_inr=median_win,
        best_trade_inr=best,
        n_losses=n_losses,
        loss_rate=loss_rate,
        avg_loss_inr=avg_loss,
        median_loss_inr=median_loss,
        worst_trade_inr=worst,
        expectancy_per_trade_inr=e,
        expectancy_per_trade_pct_of_avg_win=(e / avg_win) if avg_win > 0 else 0.0,
        profit_factor=profit_factor,
        win_loss_ratio=win_loss_ratio,
        payoff_to_breakeven_win_rate=breakeven_wr,
        worst_day_loss_inr=worst_day,
        worst_5_pct_avg_loss=worst_5pct,
        consecutive_losses_max=longest,
        consecutive_losses_avg=avg_streak,
        is_positive_expectancy=is_pos,
        expectancy_dominated_by_outliers=outlier_flag,
        has_blow_up_risk=blow_up,
        notes=notes,
    )


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2.0


def _empty_metrics() -> ExpectancyMetrics:
    return ExpectancyMetrics(
        n_trades=0, n_wins=0, win_rate=0, avg_win_inr=0, median_win_inr=0, best_trade_inr=0,
        n_losses=0, loss_rate=0, avg_loss_inr=0, median_loss_inr=0, worst_trade_inr=0,
        expectancy_per_trade_inr=0, expectancy_per_trade_pct_of_avg_win=0,
        profit_factor=0, win_loss_ratio=0, payoff_to_breakeven_win_rate=0,
        worst_day_loss_inr=0, worst_5_pct_avg_loss=0, consecutive_losses_max=0, consecutive_losses_avg=0,
        is_positive_expectancy=False, expectancy_dominated_by_outliers=False, has_blow_up_risk=False,
    )
