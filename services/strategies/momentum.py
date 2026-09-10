"""Momentum strategies: ORB 15-Min, Momentum + Volume, Weekly Momentum
Filter, Dual Thrust, Momentum-Quality Filter, MACD Histogram Divergence.
"""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. ORB 15-Min (research-backed: 8yr backtest, +91.6%, Sharpe 1.16)
# Source: intradaylab.com/blog/nifty-orb-breakout-strategy-backtest
# ------------------------------------------------------------------
class ORB15Min(Strategy):
    name = "ORB 15-Min (Research-Backed)"

    def __init__(self, sym: str, or_bars: int = 2, min_range: float = 40, tp_mult: float = 2.0):
        self._sym = sym
        self._or_bars = or_bars
        self._min_range = min_range
        self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._short = False
        self._or_high: float = 0
        self._or_low: float = 999999
        self._entry = 0.0
        self._sl = 0.0
        self._tp = 0.0
        self._bar_count = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._bar_count += 1
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        inst = bar.instrument
        qty = _lot(inst)

        if self._bar_count <= self._or_bars:
            self._or_high = max(self._or_high, bar.high)
            self._or_low = min(self._or_low, bar.low)
            return

        or_range = self._or_high - self._or_low
        if or_range < self._min_range:
            return

        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty)
                self._long = False
                self._bar_count = 0
                self._or_high = 0
                self._or_low = 999999
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty)
                self._short = False
                self._bar_count = 0
                self._or_high = 0
                self._or_low = 999999
            return

        if bar.close > self._or_high:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._entry = bar.close
            self._sl = self._or_low
            self._tp = bar.close + or_range * self._tp_mult
        elif bar.close < self._or_low:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._entry = bar.close
            self._sl = self._or_high
            self._tp = bar.close - or_range * self._tp_mult


# ------------------------------------------------------------------
# 2. Momentum + Volume Confirmation
# ------------------------------------------------------------------
class MomentumVolume(Strategy):
    name = "Momentum + Volume"

    def __init__(self, sym: str, mom_period: int = 10, vol_mult: float = 1.5, atr_period: int = 14):
        self._sym = sym; self._mom_period = mom_period
        self._vol_mult = vol_mult; self._atr_period = atr_period

    def on_start(self, ctx):
        self._closes: list[float] = []; self._volumes: list[float] = []
        self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._volumes.append(bar.volume)
        self._highs.append(bar.high); self._lows.append(bar.low)
        if len(self._closes) < self._mom_period + 1: return
        mom = bar.close - self._closes[-self._mom_period - 1]
        avg_vol = sum(self._volumes[-20:]) / min(len(self._volumes), 20) if self._volumes else 1
        high_vol = bar.volume > self._vol_mult * avg_vol if avg_vol > 0 else False
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        inst = bar.instrument; qty = _lot(inst)
        if self._long:
            if atr and bar.close < self._sl: ctx.sell(inst, quantity=qty); self._long = False
            elif atr: self._sl = max(self._sl, bar.close - 2 * atr)
            return
        if self._short:
            if atr and bar.close > self._sl: ctx.buy(inst, quantity=qty); self._short = False
            elif atr: self._sl = min(self._sl, bar.close + 2 * atr)
            return
        if not atr: return
        if mom > 0 and high_vol:
            ctx.buy(inst, quantity=qty); self._long = True; self._sl = bar.close - 2 * atr
        elif mom < 0 and high_vol:
            ctx.sell(inst, quantity=qty); self._short = True; self._sl = bar.close + 2 * atr


# ------------------------------------------------------------------
# 3. Weekly Momentum Filter (trades only 1-2x/week on strong signals)
# ------------------------------------------------------------------
class WeeklyMomentumFilter(Strategy):
    name = "Weekly Momentum Filter"

    def __init__(self, sym: str, fast: int = 5, slow: int = 20, rsi_period: int = 14,
                 rsi_confirm: float = 60, atr_period: int = 14, sl_mult: float = 1.0, tp_mult: float = 2.0):
        self._sym = sym; self._fast = fast; self._slow = slow
        self._rsi_period = rsi_period; self._rsi_confirm = rsi_confirm
        self._atr_period = atr_period; self._sl_mult = sl_mult; self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0; self._tp = 0.0
        self._bars_since_trade = 0; self._min_bars_between = 100  # ~25 bars/day * 4 days = weekly

    def _rsi(self) -> float | None:
        c = self._closes
        if len(c) < self._rsi_period + 1: return None
        gains = [max(c[i] - c[i-1], 0) for i in range(-self._rsi_period, 0)]
        losses = [max(c[i-1] - c[i], 0) for i in range(-self._rsi_period, 0)]
        ag = sum(gains) / self._rsi_period; al = sum(losses) / self._rsi_period
        return 100 - (100 / (1 + ag / al)) if al else 100.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        self._bars_since_trade += 1
        if len(self._closes) < max(self._slow, self._rsi_period + 1): return
        inst = bar.instrument; qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period) or 0
        if not atr: return

        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty); self._long = False
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty); self._short = False
            return

        if self._bars_since_trade < self._min_bars_between: return

        fast_sma = sum(self._closes[-self._fast:]) / self._fast
        slow_sma = sum(self._closes[-self._slow:]) / self._slow
        rsi = self._rsi()
        if rsi is None: return

        if fast_sma > slow_sma and rsi > self._rsi_confirm:
            ctx.buy(inst, quantity=qty); self._long = True
            self._sl = bar.close - atr * self._sl_mult; self._tp = bar.close + atr * self._tp_mult
            self._bars_since_trade = 0
        elif fast_sma < slow_sma and rsi < (100 - self._rsi_confirm):
            ctx.sell(inst, quantity=qty); self._short = True
            self._sl = bar.close + atr * self._sl_mult; self._tp = bar.close - atr * self._tp_mult
            self._bars_since_trade = 0


# ------------------------------------------------------------------
# 4. Dual Thrust (popular in Asian markets)
# ------------------------------------------------------------------
class DualThrust(Strategy):
    name = "Dual Thrust"

    def __init__(self, sym: str, lookback: int = 4, k1: float = 0.5, k2: float = 0.5):
        self._sym = sym; self._lookback = lookback; self._k1 = k1; self._k2 = k2

    def on_start(self, ctx):
        self._opens: list[float] = []; self._highs: list[float] = []
        self._lows: list[float] = []; self._closes: list[float] = []
        self._long = False; self._short = False

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._opens.append(bar.open); self._highs.append(bar.high)
        self._lows.append(bar.low); self._closes.append(bar.close)
        if len(self._closes) < self._lookback + 1: return
        hh = max(self._highs[-self._lookback - 1:-1])
        hc = max(self._closes[-self._lookback - 1:-1])
        ll = min(self._lows[-self._lookback - 1:-1])
        lc = min(self._closes[-self._lookback - 1:-1])
        rng = max(hh - lc, hc - ll)
        upper = bar.open + self._k1 * rng; lower = bar.open - self._k2 * rng
        inst = bar.instrument; qty = _lot(inst)
        if self._long and bar.close < lower:
            ctx.sell(inst, quantity=qty); self._long = False
            ctx.sell(inst, quantity=qty); self._short = True
        elif self._short and bar.close > upper:
            ctx.buy(inst, quantity=qty); self._short = False
            ctx.buy(inst, quantity=qty); self._long = True
        elif not self._long and not self._short:
            if bar.close > upper: ctx.buy(inst, quantity=qty); self._long = True
            elif bar.close < lower: ctx.sell(inst, quantity=qty); self._short = True


# ------------------------------------------------------------------
# 5. Multi-Factor Momentum-Quality (Morningstar Q2 2026 research)
# Simplified: momentum + quality filter on price action
# ------------------------------------------------------------------
class MomentumQuality(Strategy):
    name = "Momentum-Quality Filter"

    def __init__(self, sym: str, mom_period: int = 20, quality_period: int = 60):
        self._sym = sym
        self._mom = mom_period
        self._qual = quality_period

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._entry = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if len(self._closes) < self._qual:
            return

        inst = bar.instrument
        qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, 14) or 1

        mom_ret = (bar.close - self._closes[-self._mom]) / self._closes[-self._mom]
        long_sma = sum(self._closes[-self._qual:]) / self._qual
        above_trend = bar.close > long_sma
        recent = self._closes[-20:]
        consistency = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1]) / (len(recent) - 1)

        if self._long:
            if bar.close < self._entry - 1.5 * atr or mom_ret < -0.02:
                ctx.sell(inst, quantity=qty)
                self._long = False
            return

        if mom_ret > 0.02 and above_trend and consistency > 0.55:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._entry = bar.close


# ------------------------------------------------------------------
# 6. MACD Histogram Divergence
# ------------------------------------------------------------------
class MACDDivergence(Strategy):
    name = "MACD Histogram Divergence"

    def __init__(self, sym: str, fast: int = 12, slow: int = 26, signal: int = 9):
        self._sym = sym; self._fast = fast; self._slow = slow; self._signal = signal

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._macd_hist: list[float] = []; self._long = False; self._short = False

    def _ema_val(self, data: list[float], period: int) -> float | None:
        if len(data) < period: return None
        mult = 2 / (period + 1); ema = data[-period]
        for p in data[-period + 1:]: ema = p * mult + ema * (1 - mult)
        return ema

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        fast_ema = self._ema_val(self._closes, self._fast)
        slow_ema = self._ema_val(self._closes, self._slow)
        if fast_ema is None or slow_ema is None: return
        macd = fast_ema - slow_ema
        self._macd_hist.append(macd)
        if len(self._macd_hist) < self._signal + 2: return
        sig = self._ema_val(self._macd_hist, self._signal) or 0
        hist = macd - sig
        prev_hist = self._macd_hist[-2] - (self._ema_val(self._macd_hist[:-1], self._signal) or 0)
        atr = _atr(self._highs, self._lows, self._closes, 14)
        inst = bar.instrument; qty = _lot(inst)
        if self._long and hist < 0 and prev_hist >= 0:
            ctx.sell(inst, quantity=qty); self._long = False
        if self._short and hist > 0 and prev_hist <= 0:
            ctx.buy(inst, quantity=qty); self._short = False
        if not self._long and not self._short and atr:
            if hist > 0 and prev_hist <= 0:
                ctx.buy(inst, quantity=qty); self._long = True
            elif hist < 0 and prev_hist >= 0:
                ctx.sell(inst, quantity=qty); self._short = True


# ------------------------------------------------------------------
# Register all momentum strategies
# ------------------------------------------------------------------

_register("orb_15min", "ORB 15-Min (Research-Backed)",
          "8-year backtested on NIFTY (2017-2026): +91.6% return, 48.7% win rate, Sharpe 1.16, max DD -11.2%, 2,122 trades. Uses first 2x15min candles as the opening range. Buys breakout above OR high, shorts below OR low. SL at opposite OR level, TP at 2x OR range. Short trades = 75% of profits. Friday strongest, Tuesday weakest.",
          lambda sym, **kw: ORB15Min(sym, or_bars=kw.get("or_bars", 2)),
          source="intradaylab.com/blog/nifty-orb-breakout-strategy-backtest | 8yr backtest 2017-2026")

_register("momentum_volume", "Momentum + Volume Confirmation",
          "Enters when 10-bar momentum is positive/negative AND volume is 1.5x above 20-bar average (confirms institutional participation). Trailing 2xATR stop. Volume spike = smart money is moving.",
          lambda sym, **kw: MomentumVolume(sym),
          source="Mark Minervini, 'Trade Like a Stock Market Wizard' (2013) | Volume-confirmed momentum")

_register("weekly_momentum_filter", "Weekly Momentum Filter",
          "Trades max 1x/week. Requires SMA(5)>SMA(20) + RSI>60 for long (or SMA<SMA + RSI<40 for short). 100-bar cooldown between trades. Tight 1xATR SL, 2xATR TP. Low frequency = low fees = low DD.",
          lambda sym, **kw: WeeklyMomentumFilter(sym),
          source="priceactionlab.substack.com weekly ensemble (Sharpe 1.4, DD -5.2%) | Adapted for NIFTY futures")

_register("dual_thrust", "Dual Thrust (Asian Markets)",
          "Popular in Asian futures: compute range = max(HH-LC, HC-LL) over 4 bars. Buy above open+K1*range, sell below open-K2*range. Reversal system — always in the market. Widely used on Nikkei, Hang Seng, SGX.",
          lambda sym, **kw: DualThrust(sym, k1=kw.get("k1", 0.5), k2=kw.get("k2", 0.5)),
          source="futureshive.com | Dual Thrust system, adapted from Asian futures markets")

_register("momentum_quality", "Momentum-Quality Filter",
          "Buys only when 3 conditions align: positive 20-day momentum (>2%), price above 60-day SMA (long-term trend), and >55% of recent daily closes are higher than prior (consistency/quality). Exit on 1.5xATR stop or momentum reversal. Source: Morningstar Q2 2026 factor research — momentum+quality leads globally in EM.",
          lambda sym, **kw: MomentumQuality(sym))

_register("macd_divergence", "MACD Histogram Divergence",
          "Enters on MACD histogram zero-line crossover: histogram turns positive = long, negative = short. MACD(12,26,9) is the most widely used momentum oscillator. Exits on opposite crossover.",
          lambda sym, **kw: MACDDivergence(sym),
          source="Gerald Appel, 'The Moving Average Convergence-Divergence Trading Method' (1979)")
