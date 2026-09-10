"""Mean-reversion strategies: Bollinger Squeeze Breakout, Mean Reversion
Extreme (RSI + BB + volume), VWAP Reversion, Ultra-Selective Mean Reversion.
"""

from __future__ import annotations

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register


# ------------------------------------------------------------------
# 1. Bollinger Band Squeeze Breakout (volatility contraction -> expansion)
# ------------------------------------------------------------------
class BollingerSqueezeBreakout(Strategy):
    name = "Bollinger Squeeze Breakout"

    def __init__(self, sym: str, period: int = 20, std_mult: float = 2.0, squeeze_pct: float = 0.03):
        self._sym = sym
        self._period = period
        self._std_mult = std_mult
        self._squeeze_pct = squeeze_pct

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._short = False
        self._entry = 0.0
        self._sl = 0.0
        self._was_squeezed = False

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if len(self._closes) < self._period:
            return

        window = self._closes[-self._period:]
        sma = sum(window) / self._period
        std = (sum((x - sma) ** 2 for x in window) / self._period) ** 0.5
        upper = sma + self._std_mult * std
        lower = sma - self._std_mult * std
        bandwidth = (upper - lower) / sma if sma else 0

        is_squeezed = bandwidth < self._squeeze_pct
        atr = _atr(self._highs, self._lows, self._closes, min(14, self._period))
        inst = bar.instrument
        qty = _lot(inst)

        if self._long:
            if atr and bar.close < self._entry - 2 * atr:
                ctx.sell(inst, quantity=qty)
                self._long = False
            elif bar.close > upper and atr:
                self._sl = max(self._sl, bar.close - 1.5 * atr)
            return
        if self._short:
            if atr and bar.close > self._entry + 2 * atr:
                ctx.buy(inst, quantity=qty)
                self._short = False
            return

        if self._was_squeezed and not is_squeezed:
            if bar.close > upper:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
                self._sl = lower
            elif bar.close < lower:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close
                self._sl = upper

        self._was_squeezed = is_squeezed


# ------------------------------------------------------------------
# 2. Mean Reversion Extreme (RSI + Bollinger + volume confirmation)
# ------------------------------------------------------------------
class MeanReversionExtreme(Strategy):
    name = "Mean Reversion Extreme"

    def __init__(self, sym: str, bb_period: int = 20, rsi_period: int = 7, rsi_oversold: float = 25, rsi_overbought: float = 75):
        self._sym = sym
        self._bb_period = bb_period
        self._rsi_period = rsi_period
        self._rsi_os = rsi_oversold
        self._rsi_ob = rsi_overbought

    def on_start(self, ctx):
        self._closes: list[float] = []
        self._volumes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._long = False
        self._short = False
        self._entry = 0.0

    def _rsi(self, period: int) -> float | None:
        if len(self._closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(-period, 0):
            d = self._closes[i] - self._closes[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        ag = sum(gains) / period
        al = sum(losses) / period
        return 100 - (100 / (1 + ag / al)) if al else 100.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._closes.append(bar.close)
        self._volumes.append(bar.volume)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if len(self._closes) < self._bb_period:
            return

        window = self._closes[-self._bb_period:]
        sma = sum(window) / self._bb_period
        std = (sum((x - sma) ** 2 for x in window) / self._bb_period) ** 0.5
        lower_bb = sma - 2 * std
        upper_bb = sma + 2 * std
        rsi = self._rsi(self._rsi_period)
        atr = _atr(self._highs, self._lows, self._closes, 14)
        vol_avg = sum(self._volumes[-20:]) / min(len(self._volumes), 20) if self._volumes else 0
        high_volume = bar.volume > 1.2 * vol_avg if vol_avg > 0 else True
        inst = bar.instrument
        qty = _lot(inst)

        if self._long:
            if bar.close >= sma or (atr and bar.close < self._entry - 1.5 * atr):
                ctx.sell(inst, quantity=qty)
                self._long = False
            return
        if self._short:
            if bar.close <= sma or (atr and bar.close > self._entry + 1.5 * atr):
                ctx.buy(inst, quantity=qty)
                self._short = False
            return

        if rsi is not None and bar.close <= lower_bb and rsi < self._rsi_os and high_volume:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._entry = bar.close
        elif rsi is not None and bar.close >= upper_bb and rsi > self._rsi_ob and high_volume:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._entry = bar.close


# ------------------------------------------------------------------
# 3. VWAP Reversion (institutional mean-reversion)
# ------------------------------------------------------------------
class VWAPReversion(Strategy):
    name = "VWAP Reversion"

    def __init__(self, sym: str, dev_pct: float = 0.008, sl_mult: float = 1.5, tp_mult: float = 2.0):
        self._sym = sym
        self._dev_pct = dev_pct
        self._sl_mult = sl_mult
        self._tp_mult = tp_mult

    def on_start(self, ctx):
        self._cum_vol = 0.0
        self._cum_pv = 0.0
        self._long = False
        self._short = False
        self._sl = 0.0
        self._tp = 0.0
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        typical = (bar.high + bar.low + bar.close) / 3
        vol = bar.volume if bar.volume > 0 else 1
        self._cum_pv += typical * vol
        self._cum_vol += vol
        vwap = self._cum_pv / self._cum_vol if self._cum_vol > 0 else bar.close
        dev = (bar.close - vwap) / vwap if vwap else 0
        atr = _atr(self._highs, self._lows, self._closes, 14)
        inst = bar.instrument
        qty = _lot(inst)

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

        if not atr or atr == 0:
            return
        risk = atr * self._sl_mult
        if dev < -self._dev_pct:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._sl = bar.close - risk
            self._tp = vwap
        elif dev > self._dev_pct:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._sl = bar.close + risk
            self._tp = vwap


# ------------------------------------------------------------------
# 4. Ultra-Selective Mean Reversion (targets Sharpe > 2, DD < 7%)
# Trades ONLY on extreme z-score deviations with tiny position + tight SL
# ------------------------------------------------------------------
class UltraSelectiveMR(Strategy):
    name = "Ultra-Selective Mean Reversion"

    def __init__(self, sym: str, lookback: int = 40, z_entry: float = 2.5,
                 sl_atr_mult: float = 1.0, tp_atr_mult: float = 1.5):
        self._sym = sym; self._lookback = lookback; self._z_entry = z_entry
        self._sl_mult = sl_atr_mult; self._tp_mult = tp_atr_mult

    def on_start(self, ctx):
        self._closes: list[float] = []; self._highs: list[float] = []; self._lows: list[float] = []
        self._long = False; self._short = False; self._sl = 0.0; self._tp = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym: return
        self._closes.append(bar.close); self._highs.append(bar.high); self._lows.append(bar.low)
        if len(self._closes) < self._lookback: return
        inst = bar.instrument; qty = _lot(inst)
        atr = _atr(self._highs, self._lows, self._closes, 14) or 0
        if not atr: return

        if self._long:
            if bar.close <= self._sl or bar.close >= self._tp:
                ctx.sell(inst, quantity=qty); self._long = False
            return
        if self._short:
            if bar.close >= self._sl or bar.close <= self._tp:
                ctx.buy(inst, quantity=qty); self._short = False
            return

        window = self._closes[-self._lookback:]
        mean = sum(window) / self._lookback
        std = (sum((x - mean) ** 2 for x in window) / self._lookback) ** 0.5
        if std == 0: return
        z = (bar.close - mean) / std

        if z < -self._z_entry:
            ctx.buy(inst, quantity=qty); self._long = True
            self._sl = bar.close - atr * self._sl_mult
            self._tp = mean
        elif z > self._z_entry:
            ctx.sell(inst, quantity=qty); self._short = True
            self._sl = bar.close + atr * self._sl_mult
            self._tp = mean


# ------------------------------------------------------------------
# Register all mean-reversion strategies
# ------------------------------------------------------------------

_register("bollinger_squeeze", "Bollinger Squeeze Breakout",
          "Enters when Bollinger Bands squeeze (bandwidth < 3%) then expand. Breakout above upper band = long, below lower = short. Volatility compression precedes big directional moves. Low frequency, high conviction.",
          lambda sym, **kw: BollingerSqueezeBreakout(sym, period=kw.get("period", 20)),
          source="John Bollinger, 'Bollinger on Bollinger Bands' (2001) | TTM Squeeze indicator by John Carter")

_register("mean_reversion_extreme", "Mean Reversion Extreme",
          "Multi-signal confirmation: enters ONLY when price hits 2s Bollinger Band + RSI(7) < 25 or > 75 + above-average volume. Triple filter = high conviction. Exits at SMA (the mean). 1.5xATR hard stop.",
          lambda sym, **kw: MeanReversionExtreme(sym),
          source="Larry Connors, 'Short Term Trading Strategies That Work' (2008) | RSI(2) mean reversion research")

_register("vwap_reversion", "VWAP Reversion",
          "Institutional mean-reversion: buys when price drops >0.8% below VWAP, sells when >0.8% above. TP at VWAP (the mean), SL at 1.5xATR. VWAP is the volume-weighted average price — the level institutions defend.",
          lambda sym, **kw: VWAPReversion(sym, dev_pct=kw.get("dev_pct", 0.008)),
          source="Brian Shannon, 'Technical Analysis Using Multiple Timeframes' (2008) | VWAP institutional usage")

_register("ultra_selective_mr", "Ultra-Selective Mean Reversion",
          "Trades ONLY on extreme z-score deviations (>2.5σ from 40-bar mean). TP at the mean, SL at 1xATR. Ultra-selective: ~5-15 trades total over 6 months. Targets Sharpe >2, DD <10%.",
          lambda sym, **kw: UltraSelectiveMR(sym, z_entry=kw.get("z_entry", 2.5)),
          source="quantpedia.com + Elite Trader mean reversion Sharpe 2.11 backtest | z-score deviation strategy")
