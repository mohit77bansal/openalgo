"""Volatility strategies: Volatility Expansion Scalp, RSI + BB Squeeze,
NR7 Breakout (range compression/expansion).
"""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. Volatility Expansion Scalp (0DTE Gamma research + theta decay)
# Source: SSRN "0DTEs: Trading, Gamma Risk and Volatility Propagation"
# + "Trading Theta" (Lu, 2024)
# ------------------------------------------------------------------
class VolatilityExpansionScalp(Strategy):
    name = "Volatility Expansion Scalp"

    def __init__(self, sym: str, atr_period: int = 14, squeeze_bars: int = 5, expansion_mult: float = 1.8):
        self._sym = sym
        self._atr_period = atr_period
        self._squeeze_bars = squeeze_bars
        self._exp_mult = expansion_mult

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._ranges: list[float] = []
        self._long = False
        self._short = False
        self._entry = 0.0
        self._sl = 0.0
        self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        bar_range = bar.high - bar.low
        self._ranges.append(bar_range)
        if len(self._closes) < max(self._atr_period + 1, self._squeeze_bars + 1):
            return

        inst = bar.instrument
        qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr:
            return

        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty)
                self._long = False
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty)
                self._short = False
            return

        recent_avg_range = sum(self._ranges[-self._squeeze_bars:]) / self._squeeze_bars
        is_expansion = bar_range > recent_avg_range * self._exp_mult

        if is_expansion:
            if bar.close > bar.open:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
                self._sl = bar.close - atr
                self._tp = bar.close + atr * 2
            else:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close
                self._sl = bar.close + atr
                self._tp = bar.close - atr * 2


# ------------------------------------------------------------------
# 2. RSI + Bollinger Band Squeeze (double confirmation)
# ------------------------------------------------------------------
class RSIBBSqueeze(Strategy):
    name = "RSI + BB Squeeze"

    def __init__(self, sym: str, bb_period: int = 20, rsi_period: int = 14,
                 squeeze_bw: float = 0.03, rsi_bull: float = 55, rsi_bear: float = 45):
        self._sym = sym; self._bb_period = bb_period; self._rsi_period = rsi_period
        self._squeeze_bw = squeeze_bw; self._rsi_bull = rsi_bull; self._rsi_bear = rsi_bear

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._was_squeezed = False; self._sl = 0.0

    def _rsi(self, period: int) -> float | None:
        c = self._closes
        if len(c) < period + 1: return None
        gains = [max(c[i] - c[i-1], 0) for i in range(-period, 0)]
        losses = [max(c[i-1] - c[i], 0) for i in range(-period, 0)]
        ag = sum(gains) / period; al = sum(losses) / period
        return 100 - (100 / (1 + ag / al)) if al else 100.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        if len(self._closes) < max(self._bb_period, self._rsi_period + 1): return
        w = self._closes[-self._bb_period:]
        sma = sum(w) / self._bb_period
        std = (sum((x - sma)**2 for x in w) / self._bb_period)**0.5
        bw = (2 * 2 * std) / sma if sma else 0
        is_squeeze = bw < self._squeeze_bw
        rsi = self._rsi(self._rsi_period)
        atr = _atr(self._highs, self._lows, self._closes, 14) or 0
        inst = bar.instrument; qty = _lot(inst)
        if self._long:
            if bar.close < self._sl or (rsi and rsi < 40): ctx.sell(inst, quantity=qty); self._long = False
            elif atr: self._sl = max(self._sl, bar.close - 2 * atr)
            self._was_squeezed = is_squeeze; return
        if self._short:
            if bar.close > self._sl or (rsi and rsi > 60): ctx.buy(inst, quantity=qty); self._short = False
            elif atr: self._sl = min(self._sl, bar.close + 2 * atr)
            self._was_squeezed = is_squeeze; return
        if self._was_squeezed and not is_squeeze and rsi and atr:
            if rsi > self._rsi_bull: ctx.buy(inst, quantity=qty); self._long = True; self._sl = bar.close - 2 * atr
            elif rsi < self._rsi_bear: ctx.sell(inst, quantity=qty); self._short = True; self._sl = bar.close + 2 * atr
        self._was_squeezed = is_squeeze


# ------------------------------------------------------------------
# 3. Range Contraction / Expansion (NR7)
# ------------------------------------------------------------------
class NR7Breakout(Strategy):
    name = "NR7 Breakout"

    def __init__(self, sym: str, atr_period: int = 14, tp_mult: float = 2.5, sl_mult: float = 1.0):
        self._sym = sym; self._atr_period = atr_period
        self._tp_mult = tp_mult; self._sl_mult = sl_mult

    def on_start(self, ctx):
        self._ranges: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._closes: list[float] = []; self._long = False; self._short = False
        self._sl = 0.0; self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._highs.append(bar.high); self._lows.append(bar.low); self._closes.append(bar.close)
        self._ranges.append(bar.high - bar.low)
        if len(self._ranges) < 8: return
        inst = bar.instrument; qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period) or 0
        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty); self._long = False
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty); self._short = False
            return
        if not atr: return
        curr_range = self._ranges[-1]
        is_nr7 = all(curr_range <= r for r in self._ranges[-8:-1])
        if not is_nr7: return
        prev_h, prev_l = self._highs[-2], self._lows[-2]
        if bar.close > prev_h:
            ctx.buy(inst, quantity=qty); self._long = True
            self._sl = bar.close - atr * self._sl_mult; self._tp = bar.close + atr * self._tp_mult
        elif bar.close < prev_l:
            ctx.sell(inst, quantity=qty); self._short = True
            self._sl = bar.close + atr * self._sl_mult; self._tp = bar.close - atr * self._tp_mult


# ------------------------------------------------------------------
# Register all volatility strategies
# ------------------------------------------------------------------

_register("vol_expansion_scalp", "Volatility Expansion Scalp",
          "Detects bars where range expands >1.8x the recent average (volatility expansion after compression). Trades in the direction of the expansion bar with ATR-based SL (1x) and TP (2x) = 2:1 R:R. Based on 0DTE gamma research (SSRN: Dim, Eraker, Vilkov) — gamma concentration causes expansion that persists intraday.",
          lambda sym, **kw: VolatilityExpansionScalp(sym))

_register("rsi_bb_squeeze", "RSI + BB Squeeze",
          "Double confirmation: enters on Bollinger Band squeeze release (bandwidth < 3% expanding) + RSI direction (>55 long, <45 short). Trailing ATR stop. Combines volatility breakout with momentum filter.",
          lambda sym, **kw: RSIBBSqueeze(sym),
          source="John Bollinger + Larry Connors combined approach | Volatility + momentum confirmation")

_register("nr7_breakout", "NR7 Breakout (Range Compression)",
          "NR7 = Narrowest Range of last 7 bars (extreme compression). Breakout above previous high or below previous low. TP at 2.5xATR, SL at 1xATR = 2.5:1 R:R. Low frequency, high conviction.",
          lambda sym, **kw: NR7Breakout(sym),
          source="Tony Crabel, 'Day Trading with Short Term Price Patterns' (1990) | NR7 pattern research")
