"""Hybrid strategies: Regime-Adaptive Momentum/Reversion Switcher."""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. Regime-Adaptive Momentum/Reversion Switcher
# Source: SSRN "Quantitative Strategies For Momentum And Trend
# Reversal" (Charlotte Sim, Feb 2026) + Ernie Chan's regime detection
# ------------------------------------------------------------------
class RegimeAdaptive(Strategy):
    name = "Regime-Adaptive Switcher"

    def __init__(self, sym: str, lookback: int = 40, vol_window: int = 20, vol_threshold: float = 1.3):
        self._sym = sym
        self._lookback = lookback
        self._vol_window = vol_window
        self._vol_thr = vol_threshold

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._short = False
        self._entry = 0.0

    def _regime(self) -> str:
        """Detect regime: 'trending' if recent vol expanding, 'mean_rev' if contracting."""
        if len(self._closes) < self._vol_window * 2:
            return "unknown"
        recent = self._closes[-self._vol_window:]
        prior = self._closes[-self._vol_window * 2:-self._vol_window]
        r_std = (sum((x - sum(recent) / len(recent)) ** 2 for x in recent) / len(recent)) ** 0.5
        p_std = (sum((x - sum(prior) / len(prior)) ** 2 for x in prior) / len(prior)) ** 0.5
        return "trending" if p_std > 0 and r_std / p_std > self._vol_thr else "mean_rev"

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if len(self._closes) < self._lookback:
            return

        inst = bar.instrument
        qty = _lot(inst)
        regime = self._regime()
        atr = _atr(self._highs, self._lows, self._closes, 14)
        if not atr:
            return

        if self._long:
            if bar.close < self._entry - 1.5 * atr or bar.close > self._entry + 2.5 * atr:
                ctx.sell(inst, quantity=qty)
                self._long = False
            return
        if self._short:
            if bar.close > self._entry + 1.5 * atr or bar.close < self._entry - 2.5 * atr:
                ctx.buy(inst, quantity=qty)
                self._short = False
            return

        sma = sum(self._closes[-self._lookback:]) / self._lookback

        if regime == "trending":
            if bar.close > sma and not self._long:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
            elif bar.close < sma and not self._short:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close
        elif regime == "mean_rev":
            window = self._closes[-20:]
            m = sum(window) / 20
            std = (sum((x - m) ** 2 for x in window) / 20) ** 0.5
            if std > 0:
                z = (bar.close - m) / std
                if z < -1.5 and not self._long:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._entry = bar.close
                elif z > 1.5 and not self._short:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._entry = bar.close


# ------------------------------------------------------------------
# Register hybrid strategies
# ------------------------------------------------------------------

_register("regime_adaptive", "Regime-Adaptive Switcher",
          "Detects market regime (trending vs mean-reverting) by comparing recent vs prior volatility. In trending regime: follows momentum above/below SMA(40). In mean-reverting regime: trades z-score extremes (>1.5σ). Switches automatically. Source: SSRN 'Quantitative Strategies For Momentum And Trend Reversal' (Sim, 2026) + Ernie Chan regime detection.",
          lambda sym, **kw: RegimeAdaptive(sym))
