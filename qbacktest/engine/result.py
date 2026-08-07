"""Backtest result + performance metrics."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from qbacktest.core.types import Fill, Trade


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def equity_curve_metrics(
    equity: pd.Series,
    *,
    risk_free_rate: float = 0.06,
    periods_per_year: int = 252,
) -> dict[str, float]:
    """Standard performance metrics from an equity curve.

    `equity` is a pandas Series indexed by datetime (or business day).
    """
    if equity.empty or len(equity) < 2:
        return {
            "total_return": 0.0,
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "volatility": 0.0,
            "best_day": 0.0,
            "worst_day": 0.0,
        }

    equity = equity.astype(float)
    returns = equity.pct_change().dropna()

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400 if hasattr(equity.index[-1], "to_pydatetime") else 365
    years = max(days / 365.25, 1e-6)
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0 if total_return > -1 else float("nan")

    # Sharpe
    excess = returns - (risk_free_rate / periods_per_year)
    sharpe = float(np.sqrt(periods_per_year) * excess.mean() / excess.std()) if excess.std() > 0 else 0.0

    # Sortino — only downside std
    downside = returns[returns < 0]
    sortino = float(np.sqrt(periods_per_year) * (returns.mean() - risk_free_rate / periods_per_year) / downside.std()) if len(downside) > 1 and downside.std() > 0 else 0.0

    # Drawdown
    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    max_dd_pct = float(drawdown.min()) if not drawdown.empty else 0.0
    max_dd_abs = float((equity - rolling_max).min()) if not equity.empty else 0.0

    calmar = cagr / abs(max_dd_pct) if max_dd_pct < 0 else 0.0

    return {
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6) if not math.isnan(cagr) else 0.0,
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "max_drawdown": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct, 6),
        "volatility": round(float(returns.std() * np.sqrt(periods_per_year)), 6),
        "best_day": round(float(returns.max()), 6) if not returns.empty else 0.0,
        "worst_day": round(float(returns.min()), 6) if not returns.empty else 0.0,
    }


def trade_stats(trades: list[Trade]) -> dict[str, float]:
    """Win rate, avg win/loss, profit factor, expectancy."""
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
        }
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    expectancy = float(np.mean(pnls)) if pnls else 0.0
    return {
        "n_trades": len(trades),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else float("inf"),
        "expectancy": round(expectancy, 2),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
    }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class Result:
    """Backtest output. Held by the engine after run() returns."""

    strategy_name: str
    starting_capital: float
    ending_equity: float
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    daily_pnl: list[tuple[datetime, float]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    fees_total: float = 0.0
    fees_breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def equity_series(self) -> pd.Series:
        if not self.equity_curve:
            return pd.Series(dtype=float)
        idx = [t for t, _ in self.equity_curve]
        vals = [v for _, v in self.equity_curve]
        return pd.Series(vals, index=pd.DatetimeIndex(idx))

    @property
    def metrics(self) -> dict[str, Any]:
        from qbacktest.engine.expectancy import compute_expectancy
        eq = self.equity_series
        m = equity_curve_metrics(eq)
        m.update(trade_stats(self.trades))
        m["fees_total"] = round(self.fees_total, 2)
        m["starting_capital"] = self.starting_capital
        m["ending_equity"] = round(self.ending_equity, 2)
        m["net_pnl"] = round(self.ending_equity - self.starting_capital, 2)
        # Honest expectancy block
        ex = compute_expectancy(self.trades)
        m["expectancy"] = ex.to_json()
        return m

    @property
    def expectancy(self):
        from qbacktest.engine.expectancy import compute_expectancy
        return compute_expectancy(self.trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "params": self.params,
            "metrics": self.metrics,
            "fees_breakdown": self.fees_breakdown,
            "n_fills": len(self.fills),
            "n_trades": len(self.trades),
        }

    def summary(self) -> str:
        m = self.metrics
        ex = self.expectancy
        base = (
            f"Strategy:        {self.strategy_name}\n"
            f"Capital:         ₹{self.starting_capital:,.0f} → ₹{self.ending_equity:,.0f}\n"
            f"Net PnL:         ₹{m['net_pnl']:,.0f}  ({m['total_return']*100:+.2f}%)\n"
            f"Fees paid:       ₹{m['fees_total']:,.0f}\n"
            f"CAGR:            {m.get('cagr', 0)*100:.2f}%\n"
            f"Sharpe:          {m.get('sharpe', 0):.2f}\n"
            f"Sortino:         {m.get('sortino', 0):.2f}\n"
            f"Calmar:          {m.get('calmar', 0):.2f}\n"
            f"Max drawdown:    ₹{m.get('max_drawdown', 0):,.0f}  ({m.get('max_drawdown_pct', 0)*100:.2f}%)\n"
            f"Trades:          {m.get('n_trades', 0)}  ({m.get('win_rate', 0)*100:.1f}% wins)\n"
            f"Avg win/loss:    ₹{m.get('avg_win', 0):,.0f} / ₹{m.get('avg_loss', 0):,.0f}\n"
            f"Profit factor:   {m.get('profit_factor', 0):.2f}\n"
            f"Best/worst day:  {m.get('best_day', 0)*100:+.2f}% / {m.get('worst_day', 0)*100:+.2f}%\n"
        )
        return base + "\n--- Expectancy (the math that matters) ---\n" + ex.summary()

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), default=str, indent=indent)
