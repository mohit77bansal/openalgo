"""Daily-bar intraday backtest for equity screener signals.

We only have daily bars (bhavcopy), so "intraday" is modelled as: enter at the
day's OPEN, exit at the day's CLOSE, with an intraday stop approximated from the
day's LOW (long) / HIGH (short). This honestly captures gap / momentum /
breakout selection edges — which are decided at the open and resolve by close —
but NOT paths that need the tick sequence (true ORB, VWAP scalps). Stated, not
hidden.

Stop handling is deliberately conservative: if the day's adverse extreme
breached the stop level, we assume the stop filled (you were taken out), even
though a favourable close followed. That biases *against* the strategy, which is
the right way to be wrong when the intraday path is unknown.

Position sizing: equal rupee notional per trade, up to ``max_positions`` per
day, using ``leverage`` (MIS intraday margin). Costs are the real Indian equity
intraday model (STT 0.025% on sell, exchange txn, GST, SEBI, stamp, ₹20
brokerage cap) applied to both fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side


@dataclass(frozen=True, slots=True)
class Signal:
    """A screened entry for one symbol on one day."""

    date: date
    symbol: str
    direction: int          # +1 long, -1 short
    entry: float            # day open
    stop_pct: float         # fractional stop distance


@dataclass(frozen=True, slots=True)
class TradeFill:
    date: date
    symbol: str
    direction: int
    entry: float
    exit: float
    exit_reason: str
    qty: int
    notional: float
    gross_pnl: float
    costs: float
    net_pnl: float

    @property
    def net_ret(self) -> float:
        """Net return on the trade's notional — leverage/compounding-independent."""
        return self.net_pnl / self.notional if self.notional else 0.0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[TradeFill]
    equity_curve: list[tuple[date, float]]
    start_capital: float

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return round(sum(t.net_pnl for t in self.trades), 2)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades)

    @property
    def total_costs(self) -> float:
        return round(sum(t.costs for t in self.trades), 2)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.start_capital

    @property
    def total_return_pct(self) -> float:
        return round(100 * (self.final_equity - self.start_capital) / self.start_capital, 2)

    @property
    def avg_trade_ret_pct(self) -> float:
        """Mean net return per trade on notional — the clean edge measure."""
        if not self.trades:
            return 0.0
        return round(100 * sum(t.net_ret for t in self.trades) / len(self.trades), 3)

    @property
    def payoff_ratio(self) -> float:
        """Avg win size / avg loss size (on net_pnl)."""
        wins = [t.net_pnl for t in self.trades if t.net_pnl > 0]
        losses = [-t.net_pnl for t in self.trades if t.net_pnl < 0]
        if not wins or not losses:
            return 0.0
        aw = sum(wins) / len(wins)
        al = sum(losses) / len(losses)
        return round(aw / al, 2) if al else 0.0


def _resolve_exit(
    low: float, high: float, close: float, direction: int, entry: float, stop_pct: float
) -> tuple[float, str]:
    """Exit price + reason for a same-day intraday trade on a daily bar.

    Takes plain floats (not a pd.Series) — building 1M+ Series is the backtest's
    hot-path bottleneck, so callers pass a lightweight (low, high, close) tuple.
    """
    if direction > 0:
        stop_price = entry * (1 - stop_pct)
        if low <= stop_price:
            return stop_price, "stop"
        return close, "close"
    else:
        stop_price = entry * (1 + stop_pct)
        if high >= stop_price:
            return stop_price, "stop"
        return close, "close"


def _fill_costs(cost_model: CostModel, price: float, qty: int, side: Side, d: date) -> float:
    return cost_model.compute_fill(
        exchange=Exchange.NSE,
        instrument_type=InstrumentType.EQ,
        product=ProductType.INTRADAY,
        side=side,
        quantity=qty,
        price=price,
        trade_date=d,
    ).total


def backtest_signals(
    signals_by_day: dict[date, list[Signal]],
    rows_by_key: dict[tuple[date, str], pd.Series],
    *,
    start_capital: float = 100_000,
    max_positions: int = 5,
    leverage: float = 5.0,
    slip_bps: float = 5.0,
    cost_model: CostModel | None = None,
) -> BacktestResult:
    """Simulate the screened signals day by day into an equity curve.

    ``slip_bps`` charges adverse slippage on BOTH fills (per side), because the
    open/stop prints we trade at are not free — this matters most for the fade
    signals that enter at volatile gap opens. 5 bps/side ~ a realistic haircut
    for a liquid name; raise it to stress-test a fragile edge.
    """
    cm = cost_model or CostModel(brokerage=zerodha_cost_model())
    slip = slip_bps / 10_000.0
    equity = start_capital
    curve: list[tuple[date, float]] = []
    trades: list[TradeFill] = []

    # FIXED notional per trade off START capital — NOT current equity. Compounding
    # leverage makes an asymmetric edge explode (and a negative one wipe to zero),
    # which measures the sizing scheme, not the signal. Fixed sizing keeps net
    # PnL additive so the equity curve and per-trade edge reflect the screen.
    per_trade_notional = (start_capital * leverage) / max(max_positions, 1)

    for d in sorted(signals_by_day):
        day_sigs = signals_by_day[d][:max_positions]
        if not day_sigs:
            curve.append((d, equity))
            continue

        day_pnl = 0.0
        for sig in day_sigs:
            row = rows_by_key.get((d, sig.symbol))
            if row is None or sig.entry <= 0:
                continue
            qty = int(per_trade_notional // sig.entry)
            if qty <= 0:
                continue
            low, high, close = row
            exit_px, reason = _resolve_exit(low, high, close, sig.direction, sig.entry, sig.stop_pct)

            # Adverse slippage on both fills: long pays up on entry & sells low on
            # exit; short is the mirror.
            eff_entry = sig.entry * (1 + sig.direction * slip)
            eff_exit = exit_px * (1 - sig.direction * slip)
            gross = (eff_exit - eff_entry) * qty * sig.direction

            buy_px, sell_px = (eff_entry, eff_exit) if sig.direction > 0 else (eff_exit, eff_entry)
            costs = (
                _fill_costs(cm, buy_px, qty, Side.BUY, d)
                + _fill_costs(cm, sell_px, qty, Side.SELL, d)
            )
            net = gross - costs
            day_pnl += net
            trades.append(TradeFill(
                date=d, symbol=sig.symbol, direction=sig.direction,
                entry=round(sig.entry, 2), exit=round(exit_px, 2), exit_reason=reason,
                qty=qty, notional=round(qty * sig.entry, 2),
                gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
            ))

        equity += day_pnl
        curve.append((d, round(equity, 2)))

    return BacktestResult(trades=trades, equity_curve=curve, start_capital=start_capital)


def sharpe(curve: list[tuple[date, float]], start_capital: float) -> float:
    """Annualised Sharpe of daily equity returns (0 rf)."""
    if len(curve) < 3:
        return 0.0
    eq = [start_capital] + [v for _, v in curve]
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1] > 0]
    if not rets:
        return 0.0
    import statistics
    mu = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    if sd == 0:
        return 0.0
    return round((mu / sd) * (252 ** 0.5), 2)


def max_drawdown_pct(curve: list[tuple[date, float]], start_capital: float) -> float:
    peak = start_capital
    mdd = 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return round(100 * mdd, 2)
