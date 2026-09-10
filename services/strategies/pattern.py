"""Pattern-based strategies: Inside Bar Breakout, Gap Fade, Pivot Point
Breakout, Global Gap Momentum, Tight Range NR4 Breakout.
"""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. Inside Bar Breakout (price compression -> directional move)
# ------------------------------------------------------------------
class InsideBarBreakout(Strategy):
    name = "Inside Bar Breakout"

    def __init__(self, sym: str, atr_period: int = 14, sl_mult: float = 0.5, tp_mult: float = 2.0):
        self._sym = sym
        self._atr_period = atr_period
        self._sl_mult = sl_mult
        self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._long = False
        self._short = False
        self._sl = 0.0
        self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        if len(self._highs) < 2:
            return

        inst = bar.instrument
        qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period) if len(self._closes) > self._atr_period else None

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

        prev_h, prev_l = self._highs[-2], self._lows[-2]
        is_inside = bar.high <= prev_h and bar.low >= prev_l
        if not is_inside or not atr:
            return

        if bar.close > (prev_h + prev_l) / 2:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._sl = bar.close - atr * self._sl_mult
            self._tp = bar.close + atr * self._tp_mult
        else:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._sl = bar.close + atr * self._sl_mult
            self._tp = bar.close - atr * self._tp_mult


# ------------------------------------------------------------------
# 2. Gap Fade with Defined Risk (exploits NIFTY overnight gap filling)
# ------------------------------------------------------------------
class GapFade(Strategy):
    name = "Gap Fade"

    def __init__(self, sym: str, min_gap_pct: float = 0.004, sl_mult: float = 1.0, tp_mult: float = 2.0):
        self._sym = sym
        self._min_gap = min_gap_pct
        self._sl_mult = sl_mult
        self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._prev_close: float | None = None
        self._long = False
        self._short = False
        self._entry = 0.0
        self._sl = 0.0
        self._tp = 0.0
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        inst = bar.instrument
        qty = _lot(inst)

        if self._long:
            if bar.close <= self._sl:
                ctx.sell(inst, quantity=qty)
                self._long = False
            elif bar.close >= self._tp:
                ctx.sell(inst, quantity=qty)
                self._long = False
            self._prev_close = bar.close
            return
        if self._short:
            if bar.close >= self._sl:
                ctx.buy(inst, quantity=qty)
                self._short = False
            elif bar.close <= self._tp:
                ctx.buy(inst, quantity=qty)
                self._short = False
            self._prev_close = bar.close
            return

        if self._prev_close is not None:
            gap_pct = (bar.open - self._prev_close) / self._prev_close
            gap_size = abs(bar.open - self._prev_close)
            risk = gap_size * self._sl_mult

            if gap_pct > self._min_gap:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.open
                self._sl = bar.open + risk
                self._tp = bar.open - risk * self._tp_mult
            elif gap_pct < -self._min_gap:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.open
                self._sl = bar.open - risk
                self._tp = bar.open + risk * self._tp_mult

        self._prev_close = bar.close


# ------------------------------------------------------------------
# 3. Pivot Point Breakout
# ------------------------------------------------------------------
class PivotPointBreakout(Strategy):
    name = "Pivot Point Breakout"

    def __init__(self, sym: str, sl_mult: float = 1.0, tp_mult: float = 2.0):
        self._sym = sym; self._sl_mult = sl_mult; self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._prev_h: float = 0; self._prev_l: float = 99999; self._prev_c: float = 0
        self._highs: list[float] = []; self._lows: list[float] = []; self._closes: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0; self._tp = 0.0; self._bar_count = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._highs.append(bar.high); self._lows.append(bar.low); self._closes.append(bar.close)
        self._bar_count += 1
        if self._bar_count <= 1:
            self._prev_h = bar.high; self._prev_l = bar.low; self._prev_c = bar.close; return
        pivot = (self._prev_h + self._prev_l + self._prev_c) / 3
        r1 = 2 * pivot - self._prev_l; s1 = 2 * pivot - self._prev_h
        r2 = pivot + (self._prev_h - self._prev_l); s2 = pivot - (self._prev_h - self._prev_l)
        inst = bar.instrument; qty = _lot(inst)
        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp: ctx.sell(inst, quantity=qty); self._long = False
        elif self._short:
            if bar.close >= self._sl or bar.close <= self._tp: ctx.buy(inst, quantity=qty); self._short = False
        elif bar.close > r1:
            risk = bar.close - pivot
            ctx.buy(inst, quantity=qty); self._long = True
            self._sl = bar.close - risk * self._sl_mult; self._tp = bar.close + risk * self._tp_mult
        elif bar.close < s1:
            risk = pivot - bar.close
            ctx.sell(inst, quantity=qty); self._short = True
            self._sl = bar.close + risk * self._sl_mult; self._tp = bar.close - risk * self._tp_mult
        self._prev_h = bar.high; self._prev_l = bar.low; self._prev_c = bar.close


# ------------------------------------------------------------------
# 4. Overnight Gap + Global Sentiment
# Uses previous close vs open to infer global impact
# ------------------------------------------------------------------
class GlobalGapMomentum(Strategy):
    name = "Global Gap Momentum"

    def __init__(self, sym: str, gap_threshold: float = 0.003, big_gap: float = 0.008,
                 sl_mult: float = 1.0, tp_mult: float = 2.0):
        self._sym = sym
        self._gap_thr = gap_threshold
        self._big_gap = big_gap
        self._sl_mult = sl_mult
        self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._prev_close: float | None = None
        self._long = False
        self._short = False
        self._sl = 0.0
        self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        inst = bar.instrument
        qty = _lot(inst)

        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty)
                self._long = False
            self._prev_close = bar.close
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty)
                self._short = False
            self._prev_close = bar.close
            return

        if self._prev_close:
            gap_pct = (bar.open - self._prev_close) / self._prev_close
            gap_abs = abs(bar.open - self._prev_close)
            risk = gap_abs * self._sl_mult

            if abs(gap_pct) >= self._big_gap:
                if gap_pct > 0:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._sl = bar.open - risk
                    self._tp = bar.open + risk * self._tp_mult
                else:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._sl = bar.open + risk
                    self._tp = bar.open - risk * self._tp_mult
            elif abs(gap_pct) >= self._gap_thr:
                if gap_pct > 0:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._sl = bar.open + risk
                    self._tp = bar.open - risk * self._tp_mult
                else:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._sl = bar.open - risk
                    self._tp = bar.open + risk * self._tp_mult

        self._prev_close = bar.close


# ------------------------------------------------------------------
# 5. Tight Range Breakout (NR4 + volume, ultra-selective)
# ------------------------------------------------------------------
class TightRangeNR4(Strategy):
    name = "Tight Range NR4 Breakout"

    def __init__(self, sym: str, atr_period: int = 14, sl_mult: float = 0.5, tp_mult: float = 1.5):
        self._sym = sym; self._atr_period = atr_period
        self._sl_mult = sl_mult; self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._ranges: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._closes: list[float] = []; self._volumes: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0; self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._highs.append(bar.high); self._lows.append(bar.low)
        self._closes.append(bar.close); self._volumes.append(bar.volume)
        self._ranges.append(bar.high - bar.low)
        if len(self._ranges) < 5: return
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

        # NR4: narrowest range of last 4 bars
        curr = self._ranges[-1]
        is_nr4 = all(curr <= r for r in self._ranges[-5:-1])
        if not is_nr4: return

        # Volume confirmation: below average (compression)
        avg_vol = sum(self._volumes[-20:]) / min(len(self._volumes), 20) if self._volumes else 1
        if bar.volume > avg_vol: return  # want low-volume compression, not expansion

        prev_h, prev_l = self._highs[-2], self._lows[-2]
        if bar.close > prev_h:
            ctx.buy(inst, quantity=qty); self._long = True
            self._sl = bar.close - atr * self._sl_mult; self._tp = bar.close + atr * self._tp_mult
        elif bar.close < prev_l:
            ctx.sell(inst, quantity=qty); self._short = True
            self._sl = bar.close + atr * self._sl_mult; self._tp = bar.close - atr * self._tp_mult


# ------------------------------------------------------------------
# Register all pattern strategies
# ------------------------------------------------------------------

_register("inside_bar_breakout", "Inside Bar Breakout",
          "Detects inside bars (today's range inside yesterday's = price compression). Trades the breakout direction with ATR-based SL (0.5xATR) and TP (2xATR) = 4:1 R:R. Low frequency, high selectivity.",
          lambda sym, **kw: InsideBarBreakout(sym),
          source="Al Brooks, 'Trading Price Action' (2012) | Candlestick pattern research, Thomas Bulkowski")

_register("gap_fade", "Gap Fade (2:1 R:R)",
          "Fades overnight gaps > 0.4%. SL = 1x gap size, TP = 2x gap size = structural 2:1 R:R. NIFTY fills ~65% of gaps within the day. Needs only 34% win rate to profit.",
          lambda sym, **kw: GapFade(sym, min_gap_pct=kw.get("min_gap_pct", 0.004)),
          source="equitypandit.com/giftnifty | NIFTY gap-fill statistics 2015-2025")

_register("pivot_breakout", "Pivot Point Breakout",
          "Classical pivot points: buy above R1 (first resistance), sell below S1 (first support). SL at pivot, TP at 2x risk = 2:1 R:R. Pivot = (H+L+C)/3. Widely used by floor traders and institutions.",
          lambda sym, **kw: PivotPointBreakout(sym),
          source="Floor trader pivot system | Used by CME floor traders since 1970s")

_register("global_gap_momentum", "Global Gap Momentum",
          "Exploits overnight global-market impact on NIFTY open. Small gaps (0.3-0.8%) = fade (65% fill within day). Big gaps (>0.8%) = ride momentum (don't fill same day). GIFT Nifty predicts NIFTY open direction 85-90% of the time. 2:1 R:R.",
          lambda sym, **kw: GlobalGapMomentum(sym, gap_threshold=kw.get("gap_threshold", 0.003)),
          source="marketnetra.in/blog/sgx-nifty-gift-nifty-pre-market-guide | GIFT Nifty correlation research")

_register("tight_range_nr4", "Tight Range NR4 Breakout",
          "NR4 (narrowest range of 4 bars) + below-average volume (double compression). Breakout with 0.5xATR SL, 1.5xATR TP = 3:1 R:R. Ultra-selective: only fires on extreme compression. Low frequency.",
          lambda sym, **kw: TightRangeNR4(sym),
          source="Tony Crabel NR4 variant + volume compression filter | Targets low-DD breakouts")
