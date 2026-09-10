"""Trend-following strategies: ATR Channel Breakout, EMA Crossover, Donchian
(Turtle) Breakout, Supertrend + RSI, Keltner Channel, MABB, Heikin-Ashi Trend.
"""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. ATR Channel Breakout (Turtle-style, 2:1 R:R via ATR trailing)
# ------------------------------------------------------------------
class ATRChannelBreakout(Strategy):
    name = "ATR Channel Breakout"

    def __init__(self, sym: str, lookback: int = 20, atr_period: int = 14, atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0):
        self._sym = sym
        self._lookback = lookback
        self._atr_period = atr_period
        self._sl_mult = atr_sl_mult
        self._tp_mult = atr_tp_mult

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
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
        if len(self._closes) < max(self._lookback, self._atr_period + 1):
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return
        inst = bar.instrument
        qty = _lot(inst)
        ch_high = max(self._highs[-self._lookback - 1:-1])
        ch_low = min(self._lows[-self._lookback - 1:-1])

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

        if bar.close > ch_high:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._entry = bar.close
            self._sl = bar.close - atr * self._sl_mult
            self._tp = bar.close + atr * self._tp_mult
        elif bar.close < ch_low:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._entry = bar.close
            self._sl = bar.close + atr * self._sl_mult
            self._tp = bar.close - atr * self._tp_mult


# ------------------------------------------------------------------
# 2. EMA Crossover with ATR Trailing Stop
# ------------------------------------------------------------------
class EMACrossoverATR(Strategy):
    name = "EMA Crossover + ATR Trail"

    def __init__(self, sym: str, fast: int = 9, slow: int = 21, atr_period: int = 14, trail_mult: float = 2.0):
        self._sym = sym; self._fast = fast; self._slow = slow
        self._atr_period = atr_period; self._trail_mult = trail_mult

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0

    def _ema(self, period: int, closes: list[float] | None = None) -> float | None:
        closes = self._closes if closes is None else closes
        if len(closes) < period: return None
        mult = 2 / (period + 1); ema = closes[-period]
        for p in closes[-period + 1:]: ema = p * mult + ema * (1 - mult)
        return ema

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        fast = self._ema(self._fast); slow = self._ema(self._slow)
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not fast or not slow or not atr: return
        inst = bar.instrument; qty = _lot(inst)
        if self._long:
            self._sl = max(self._sl, bar.close - self._trail_mult * atr)
            if bar.close < self._sl: ctx.sell(inst, quantity=qty); self._long = False
            return
        if self._short:
            self._sl = min(self._sl, bar.close + self._trail_mult * atr)
            if bar.close > self._sl: ctx.buy(inst, quantity=qty); self._short = False
            return
        # Crossover = fast/slow relationship FLIPPED vs the previous bar. Compare
        # against EMAs computed WITHOUT the latest close (the old code recomputed
        # the current fast EMA, making the entry condition self-contradictory →
        # it never fired).
        prev_closes = self._closes[:-1]
        prev_fast = self._ema(self._fast, prev_closes)
        prev_slow = self._ema(self._slow, prev_closes)
        crossed_up = prev_fast is None or prev_slow is None or prev_fast <= prev_slow
        crossed_down = prev_fast is None or prev_slow is None or prev_fast >= prev_slow
        if fast > slow and crossed_up:
            ctx.buy(inst, quantity=qty); self._long = True; self._sl = bar.close - self._trail_mult * atr
        elif fast < slow and crossed_down:
            ctx.sell(inst, quantity=qty); self._short = True; self._sl = bar.close + self._trail_mult * atr


# ------------------------------------------------------------------
# 3. Donchian Channel Breakout (20/10) — original Turtle system
# ------------------------------------------------------------------
class DonchianBreakout(Strategy):
    name = "Donchian Channel Breakout"

    def __init__(self, sym: str, entry_period: int = 20, exit_period: int = 10):
        self._sym = sym; self._entry = entry_period; self._exit = exit_period

    def on_start(self, ctx):
        self._highs: list[float] = []; self._lows: list[float] = []; self._closes: list[float] = []
        self._long = False; self._short = False

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._highs.append(bar.high); self._lows.append(bar.low); self._closes.append(bar.close)
        if len(self._highs) < self._entry + 1: return
        inst = bar.instrument; qty = _lot(inst)
        entry_high = max(self._highs[-self._entry - 1:-1])
        entry_low = min(self._lows[-self._entry - 1:-1])
        exit_high = max(self._highs[-self._exit - 1:-1]) if len(self._highs) > self._exit else entry_high
        exit_low = min(self._lows[-self._exit - 1:-1]) if len(self._lows) > self._exit else entry_low
        if self._long and bar.close < exit_low: ctx.sell(inst, quantity=qty); self._long = False
        if self._short and bar.close > exit_high: ctx.buy(inst, quantity=qty); self._short = False
        if not self._long and not self._short:
            if bar.close > entry_high: ctx.buy(inst, quantity=qty); self._long = True
            elif bar.close < entry_low: ctx.sell(inst, quantity=qty); self._short = True


# ------------------------------------------------------------------
# 4. Supertrend + RSI Confirmation
# ------------------------------------------------------------------
class SupertrendRSI(Strategy):
    name = "Supertrend + RSI"

    def __init__(self, sym: str, st_period: int = 10, st_mult: float = 3.0, rsi_period: int = 14):
        self._sym = sym
        self._st_period = st_period
        self._st_mult = st_mult
        self._rsi_period = rsi_period

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._final_upper: float | None = None
        self._final_lower: float | None = None
        self._st_trend: int = 1
        self._long = False
        self._short = False

    def _rsi(self) -> float | None:
        c = self._closes
        if len(c) < self._rsi_period + 1: return None
        gains, losses = [], []
        for i in range(-self._rsi_period, 0):
            d = c[i] - c[i - 1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        ag = sum(gains) / self._rsi_period; al = sum(losses) / self._rsi_period
        return 100 - (100 / (1 + ag / al)) if al else 100.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        if len(self._closes) < self._st_period + 1: return
        atr = _atr(self._highs, self._lows, self._closes, self._st_period) or 0
        if not atr: return
        # Standard Supertrend: ratcheting final bands + flip when close crosses
        # the current trend's band. (Old code seeded bands at 0 and never let
        # the trend flip, so no entry ever fired.)
        hl2 = (bar.high + bar.low) / 2
        basic_upper = hl2 + self._st_mult * atr
        basic_lower = hl2 - self._st_mult * atr
        prev_close = self._closes[-2]
        if self._final_upper is None:
            self._final_upper, self._final_lower = basic_upper, basic_lower
        else:
            self._final_upper = (basic_upper if (basic_upper < self._final_upper or prev_close > self._final_upper)
                                 else self._final_upper)
            self._final_lower = (basic_lower if (basic_lower > self._final_lower or prev_close < self._final_lower)
                                 else self._final_lower)
        prev_trend = self._st_trend
        if self._st_trend == 1 and bar.close < self._final_lower:
            self._st_trend = -1
        elif self._st_trend == -1 and bar.close > self._final_upper:
            self._st_trend = 1
        rsi = self._rsi()
        inst = bar.instrument; qty = _lot(inst)
        if self._long and self._st_trend == -1:
            ctx.sell(inst, quantity=qty); self._long = False
        if self._short and self._st_trend == 1:
            ctx.buy(inst, quantity=qty); self._short = False
        if not self._long and not self._short:
            if self._st_trend == 1 and prev_trend == -1 and rsi and rsi > 50:
                ctx.buy(inst, quantity=qty); self._long = True
            elif self._st_trend == -1 and prev_trend == 1 and rsi and rsi < 50:
                ctx.sell(inst, quantity=qty); self._short = True


# ------------------------------------------------------------------
# 5. Keltner Channel Mean Reversion
# ------------------------------------------------------------------
class KeltnerReversion(Strategy):
    name = "Keltner Channel Reversion"

    def __init__(self, sym: str, ema_period: int = 20, atr_period: int = 10, atr_mult: float = 2.0):
        self._sym = sym; self._ema_period = ema_period
        self._atr_period = atr_period; self._atr_mult = atr_mult

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._entry = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        if len(self._closes) < self._ema_period: return
        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr: return
        ema = sum(self._closes[-self._ema_period:]) / self._ema_period
        upper = ema + self._atr_mult * atr; lower = ema - self._atr_mult * atr
        inst = bar.instrument; qty = _lot(inst)
        if self._long:
            if bar.close >= ema or bar.close < self._entry - 2 * atr:
                ctx.sell(inst, quantity=qty); self._long = False
            return
        if self._short:
            if bar.close <= ema or bar.close > self._entry + 2 * atr:
                ctx.buy(inst, quantity=qty); self._short = False
            return
        if bar.close < lower: ctx.buy(inst, quantity=qty); self._long = True; self._entry = bar.close
        elif bar.close > upper: ctx.sell(inst, quantity=qty); self._short = True; self._entry = bar.close


# ------------------------------------------------------------------
# 6. MABB — Moving Average Bollinger Bands (15yr Bank Nifty backtest)
# Source: financewithsai.com/s06-banknifty-mabb-intraday-trading-strategy
# ------------------------------------------------------------------
class MABB(Strategy):
    name = "MABB (Research-Backed)"

    def __init__(self, sym: str, bb_period: int = 20, sma_period: int = 200,
                 atr_entry: int = 30, atr_sl: int = 500, lookback: int = 24):
        self._sym = sym
        self._bb_period = bb_period
        self._sma_period = sma_period
        self._atr_entry = atr_entry
        self._atr_sl = atr_sl
        self._lookback = lookback

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._short = False
        self._sl = 0.0

    def _hlc3(self, i: int) -> float:
        return (self._highs[i] + self._lows[i] + self._closes[i]) / 3

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        n = len(self._closes)
        if n < max(self._sma_period, self._bb_period, self._atr_entry + 1, self._lookback + 1):
            return

        inst = bar.instrument
        qty = _lot(inst)
        atr_e = _atr(self._highs, self._lows, self._closes, self._atr_entry) or 0
        atr_s = _atr(self._highs, self._lows, self._closes, min(self._atr_sl, n - 1)) or atr_e

        bb_window = self._closes[-self._bb_period:]
        bb_sma = sum(bb_window) / self._bb_period
        bb_std = (sum((x - bb_sma) ** 2 for x in bb_window) / self._bb_period) ** 0.5
        upper_bb = bb_sma + 2 * bb_std
        lower_bb = bb_sma - 2 * bb_std

        hlc3_vals = [self._hlc3(i) for i in range(-self._sma_period, 0)]
        sma200 = sum(hlc3_vals) / self._sma_period

        highest_close = max(self._closes[-self._lookback - 1:-1])
        lowest_close = min(self._closes[-self._lookback - 1:-1])

        if self._long:
            if bar.close <= self._sl:
                ctx.sell(inst, quantity=qty)
                self._long = False
            return
        if self._short:
            if bar.close >= self._sl:
                ctx.buy(inst, quantity=qty)
                self._short = False
            return

        if bar.close > sma200 and bar.close > upper_bb:
            trigger = highest_close + 2 * atr_e
            if bar.close >= trigger:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._sl = bar.close - 2.5 * atr_s
        elif bar.close < sma200 and bar.close < lower_bb:
            trigger = lowest_close - 2 * atr_e
            if bar.close <= trigger:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._sl = bar.close + 2.5 * atr_s


# ------------------------------------------------------------------
# 7. Heikin-Ashi Trend Follower (smoothed candle momentum)
# ------------------------------------------------------------------
class HeikinAshiTrend(Strategy):
    name = "Heikin-Ashi Trend"

    def __init__(self, sym: str, confirm_bars: int = 3, atr_period: int = 14, sl_mult: float = 2.0):
        self._sym = sym
        self._confirm = confirm_bars
        self._atr_period = atr_period
        self._sl_mult = sl_mult

    def on_start(self, ctx):
        self._ha_close: list[float] = []
        self._ha_open: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._long = False
        self._short = False
        self._sl = 0.0
        self._bull_count = 0
        self._bear_count = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        ha_c = (bar.open + bar.high + bar.low + bar.close) / 4
        ha_o = (self._ha_open[-1] + self._ha_close[-1]) / 2 if self._ha_open else (bar.open + bar.close) / 2
        self._ha_close.append(ha_c)
        self._ha_open.append(ha_o)

        is_bull = ha_c > ha_o
        is_bear = ha_c < ha_o
        self._bull_count = self._bull_count + 1 if is_bull else 0
        self._bear_count = self._bear_count + 1 if is_bear else 0

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        inst = bar.instrument
        qty = _lot(inst)

        if self._long:
            if self._bear_count >= 2 or (atr and bar.close < self._sl):
                ctx.sell(inst, quantity=qty)
                self._long = False
            elif atr:
                self._sl = max(self._sl, bar.close - self._sl_mult * atr)
            return
        if self._short:
            if self._bull_count >= 2 or (atr and bar.close > self._sl):
                ctx.buy(inst, quantity=qty)
                self._short = False
            elif atr:
                self._sl = min(self._sl, bar.close + self._sl_mult * atr)
            return

        if not atr:
            return
        if self._bull_count >= self._confirm:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._sl = bar.close - self._sl_mult * atr
        elif self._bear_count >= self._confirm:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._sl = bar.close + self._sl_mult * atr


# ------------------------------------------------------------------
# Register all trend-following strategies
# ------------------------------------------------------------------

_register("atr_channel_breakout", "ATR Channel Breakout",
          "Turtle-style: buy on 20-day high breakout, sell on 20-day low. SL at 1.5xATR, TP at 3xATR = structural 2:1 R:R. Catches big trends, gives back on chop.",
          lambda sym, **kw: ATRChannelBreakout(sym, lookback=kw.get("lookback", 20)),
          source="Curtis Faith, 'Way of the Turtle' (2007) | Richard Dennis Turtle Trading experiment 1983-1988")

_register("ema_crossover_atr", "EMA Crossover + ATR Trail",
          "Fast EMA(9) crosses slow EMA(21) = trend change. Trailing stop at 2xATR protects profits. Combines trend detection with dynamic risk management.",
          lambda sym, **kw: EMACrossoverATR(sym, fast=kw.get("fast", 9), slow=kw.get("slow", 21)),
          source="futureshive.com/blog/futures-trading-strategies-indicators-2025 | EMA+ATR strategy")

_register("donchian_breakout", "Donchian Channel Breakout",
          "Enter on 20-period high/low breakout (Donchian channel). Exit on 10-period opposite channel. Classic channel breakout — simpler than Turtle but same principle. Tends to catch large moves.",
          lambda sym, **kw: DonchianBreakout(sym, entry_period=kw.get("entry", 20), exit_period=kw.get("exit", 10)),
          source="Richard Donchian, 'Trend Following' | Original channel breakout system 1960s")

_register("supertrend_rsi", "Supertrend + RSI",
          "Enters on Supertrend direction change confirmed by RSI>50 (long) or RSI<50 (short). Supertrend(10,3) filters noise; RSI(14) confirms momentum. Exits on opposite Supertrend flip.",
          lambda sym, **kw: SupertrendRSI(sym),
          source="onetradejournal.com/indicators/supertrend-indicator-guide | Supertrend backtests 2009-2024")

_register("keltner_reversion", "Keltner Channel Reversion",
          "Mean-reversion at Keltner Channel extremes: buy at lower band (EMA - 2xATR), sell at upper band. TP at the EMA (mean). Hard stop at 2xATR beyond entry. Works in ranging markets.",
          lambda sym, **kw: KeltnerReversion(sym),
          source="Chester Keltner (1960) + Linda Bradford Raschke modernization | Channel-based mean reversion")

_register("mabb", "MABB (Research-Backed)",
          "Moving Average Bollinger Bands — 15-year backtested on Bank Nifty. Entry: close > SMA(200) + above upper BB(20,2) + highest close(24) + 2xATR(30). SL: 2.5xATR(500). Filters for strong momentum breakouts only.",
          lambda sym, **kw: MABB(sym),
          source="financewithsai.com/s06-banknifty-mabb-intraday-trading-strategy | 15yr Bank Nifty backtest")

_register("heikin_ashi_trend", "Heikin-Ashi Trend",
          "Smoothed candle momentum: enters after 3 consecutive Heikin-Ashi bullish/bearish candles (filters noise). Trailing stop at 2xATR. Exits on 2 consecutive opposite HA candles. Trend-following with noise reduction.",
          lambda sym, **kw: HeikinAshiTrend(sym, confirm_bars=kw.get("confirm_bars", 3)),
          source="Dan Valcu, 'Using Heikin-Ashi Technique' (2004) | Stocks & Commodities Magazine")
