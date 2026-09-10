"""Research-backed options strategies for BANKNIFTY weekly options on 1-minute bars.

Five intraday strategies distilled from published backtests and research papers.
All designed for 1-minute bar data, option BUYING only (max loss = premium paid),
sharing the ATR-based premium proxy from the options strategy suite.

Each strategy tracks premium P&L using the delta ~0.5 approximation:
  premium_change ~= 0.5 * underlying_move * direction

Premium proxy: 0.4 * ATR(14) -- reasonable for ATM weekly options on volatile
indices like BANKNIFTY.

Sources cited in each strategy class docstring and registration.
"""

from __future__ import annotations

import math

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr
from ._registry import _register

# -- Constants ----------------------------------------------------------------

_EXPIRY_WEEKDAY = 3          # Thursday
_PREMIUM_ATR_FRAC = 0.4      # ATM weekly premium ~ 0.4 * ATR(14)


# -- Shared helpers -----------------------------------------------------------

def _rsi(closes: list[float], period: int) -> float | None:
    """Wilder's RSI over the last *period* + 1 closes."""
    if len(closes) < period + 1:
        return None
    changes: list[float] = []
    for i in range(-period, 0):
        changes.append(closes[i] - closes[i - 1])
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _is_expiry_cutoff(bar_ts) -> bool:
    """True if bar is on expiry day (Thursday) at or after 14:30."""
    if bar_ts.weekday() != _EXPIRY_WEEKDAY:
        return False
    return bar_ts.hour > 14 or (bar_ts.hour == 14 and bar_ts.minute >= 30)


def _current_premium(entry_premium: float, entry_price: float,
                     current_price: float, direction: int) -> float:
    """Estimate current option premium from cumulative underlying movement.

    direction: +1 for CE (call), -1 for PE (put).
    Delta ~0.5 approximation: premium moves = 0.5 * underlying move.
    """
    underlying_move = (current_price - entry_price) * direction
    return max(entry_premium + underlying_move * 0.5, 0.0)


# =====================================================================
# 1. ORB Options (Opening Range Breakout)
# =====================================================================

class ORBOptions(Strategy):
    """Opening Range Breakout on 1-minute options.

    Source: IntradayLab 8-year backtest, Sharpe 1.16, 48.7% win rate.

    Build opening range from first 30 bars (9:15-9:45 on 1m data).
    Breakout above OR high -> buy CE, below OR low -> buy PE.
    SL: 20% of entry premium.  TP: 2x risk (40% premium gain).
    Time exit: bar 315 from session start (~14:30).
    Max 1 trade per day.  Thursday 14:30 expiry cutoff.
    """

    name = "ORB Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        or_bars: int = 30,
        sl_pct: float = 0.20,
        tp_pct: float = 0.40,
        exit_bar: int = 315,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._or_bars = or_bars
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._exit_bar = exit_bar

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._current_date = None
        self._session_bar_count = 0
        self._or_high = 0.0
        self._or_low = float("inf")
        self._or_built = False

        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0
        self._traded_today = False

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        # -- Session tracking (reset on new date) --
        bar_date = bar.ts.date()
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._session_bar_count = 0
            self._or_high = 0.0
            self._or_low = float("inf")
            self._or_built = False
            self._traded_today = False
            if self._in_trade:
                ctx.sell(bar.instrument, quantity=1)
                self._in_trade = False
                self._direction = 0

        self._session_bar_count += 1

        if len(self._closes) < self._atr_period + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument

        # -- Build opening range from first N bars --
        if self._session_bar_count <= self._or_bars:
            self._or_high = max(self._or_high, bar.high)
            self._or_low = min(self._or_low, bar.low)
            if self._session_bar_count == self._or_bars:
                self._or_built = True
            return

        if not self._or_built:
            return

        # -- Exit logic --
        if self._in_trade:
            current = _current_premium(
                self._entry_premium, self._entry_underlying,
                bar.close, self._direction,
            )
            should_exit = (
                current >= self._entry_premium * (1.0 + self._tp_pct)
                or current <= self._entry_premium * (1.0 - self._sl_pct)
                or self._session_bar_count >= self._exit_bar
                or _is_expiry_cutoff(bar.ts)
            )
            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # -- Entry logic --
        if self._traded_today or self._session_bar_count >= self._exit_bar:
            return

        premium = _PREMIUM_ATR_FRAC * atr

        if bar.close > self._or_high:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._traded_today = True
        elif bar.close < self._or_low:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._traded_today = True


# =====================================================================
# 2. VWAP Pullback Scalp
# =====================================================================

class VWAPPullback(Strategy):
    """VWAP Pullback Scalp on 1-minute options.

    Source: OneTradeJournal, claimed Sharpe 1.8, 68% win rate.

    Compute session VWAP (cumulative typical_price*volume / cum_volume).
    Price above VWAP pulls back to within 0.15% -> buy CE.
    Price below VWAP pulls back to within 0.15% -> buy PE.
    Confirmation: reversal candle (close on the trend side of open).
    SL: 15% of premium.  TP: ATR-scaled 15-point equivalent.
    Skip midday (bars 135-270 from session, ~11:30-13:30).
    Max 3 trades per day.  Thursday 14:30 expiry cutoff.
    """

    name = "VWAP Pullback Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        pullback_pct: float = 0.15,
        sl_pct: float = 0.15,
        tp_atr_frac: float = 0.15,
        max_trades: int = 3,
        midday_start_bar: int = 135,
        midday_end_bar: int = 270,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._pullback_pct = pullback_pct / 100.0  # 0.15% -> 0.0015
        self._sl_pct = sl_pct
        self._tp_atr_frac = tp_atr_frac
        self._max_trades = max_trades
        self._midday_start = midday_start_bar
        self._midday_end = midday_end_bar

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._current_date = None
        self._session_bar_count = 0
        self._cum_pv = 0.0
        self._cum_vol = 0.0

        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0
        self._tp_level = 0.0
        self._trades_today = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        # -- Session tracking (reset on new date) --
        bar_date = bar.ts.date()
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._session_bar_count = 0
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._trades_today = 0
            if self._in_trade:
                ctx.sell(bar.instrument, quantity=1)
                self._in_trade = False
                self._direction = 0

        self._session_bar_count += 1

        # -- Update session VWAP --
        vol = getattr(bar, "volume", 0.0) or 1.0  # equal-weight fallback
        typical = (bar.high + bar.low + bar.close) / 3.0
        self._cum_pv += typical * vol
        self._cum_vol += vol

        if self._cum_vol == 0:
            return

        vwap = self._cum_pv / self._cum_vol

        if len(self._closes) < self._atr_period + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument

        # -- Exit logic --
        if self._in_trade:
            current = _current_premium(
                self._entry_premium, self._entry_underlying,
                bar.close, self._direction,
            )
            should_exit = (
                current >= self._tp_level
                or current <= self._entry_premium * (1.0 - self._sl_pct)
                or _is_expiry_cutoff(bar.ts)
            )
            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # -- Entry logic --
        if self._trades_today >= self._max_trades:
            return

        # Skip midday low-volume zone
        if self._midday_start <= self._session_bar_count <= self._midday_end:
            return

        if vwap == 0:
            return

        # Check pullback: price within 0.15% of VWAP
        distance_pct = abs(bar.close - vwap) / vwap
        if distance_pct > self._pullback_pct:
            return

        bar_open = getattr(
            bar, "open",
            self._closes[-2] if len(self._closes) >= 2 else bar.close,
        )
        premium = _PREMIUM_ATR_FRAC * atr
        tp_offset = self._tp_atr_frac * atr

        # CE: price at/above VWAP, bullish reversal candle (close >= open)
        if bar.close >= vwap and bar.close >= bar_open:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._tp_level = premium + tp_offset
            self._trades_today += 1
        # PE: price at/below VWAP, bearish reversal candle (close <= open)
        elif bar.close <= vwap and bar.close <= bar_open:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._tp_level = premium + tp_offset
            self._trades_today += 1


# =====================================================================
# 3. EMA 9/21 Crossover Scalp
# =====================================================================

class EMACrossoverScalp(Strategy):
    """EMA 9/21 Crossover Scalp on 1-minute options.

    Source: Tradejini Technical Playbook.

    9 EMA crosses above 21 EMA -> buy CE.
    9 EMA crosses below 21 EMA -> buy PE.
    Confirmation: RSI(14) > 55 for CE, < 45 for PE.
    SL: 15% of premium.  TP: 30% premium gain.
    Max hold: 30 bars (30 min on 1m).  Max 5 trades per day.
    Thursday 14:30 expiry cutoff.
    """

    name = "EMA Crossover Scalp Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        rsi_long: float = 55.0,
        rsi_short: float = 45.0,
        sl_pct: float = 0.15,
        tp_pct: float = 0.30,
        max_hold: int = 30,
        max_trades: int = 5,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow
        self._rsi_period = rsi_period
        self._rsi_long = rsi_long
        self._rsi_short = rsi_short
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._max_hold = max_hold
        self._max_trades = max_trades
        self._k_fast = 2.0 / (ema_fast + 1)
        self._k_slow = 2.0 / (ema_slow + 1)

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._ema_fast_val: float | None = None
        self._ema_slow_val: float | None = None

        self._current_date = None
        self._bar_count = 0
        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0
        self._entry_bar_idx = 0
        self._trades_today = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

        # -- Daily reset --
        bar_date = bar.ts.date()
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._trades_today = 0
            if self._in_trade:
                ctx.sell(bar.instrument, quantity=1)
                self._in_trade = False
                self._direction = 0

        n = len(self._closes)

        # -- Update EMAs (save previous for crossover detection) --
        prev_fast = self._ema_fast_val
        prev_slow = self._ema_slow_val

        if n == self._ema_fast_period:
            self._ema_fast_val = (
                sum(self._closes[-self._ema_fast_period:]) / self._ema_fast_period
            )
        elif n > self._ema_fast_period and self._ema_fast_val is not None:
            self._ema_fast_val = (
                bar.close * self._k_fast
                + self._ema_fast_val * (1.0 - self._k_fast)
            )

        if n == self._ema_slow_period:
            self._ema_slow_val = (
                sum(self._closes[-self._ema_slow_period:]) / self._ema_slow_period
            )
        elif n > self._ema_slow_period and self._ema_slow_val is not None:
            self._ema_slow_val = (
                bar.close * self._k_slow
                + self._ema_slow_val * (1.0 - self._k_slow)
            )

        # Need both current and previous EMA values for crossover
        if (self._ema_fast_val is None or self._ema_slow_val is None
                or prev_fast is None or prev_slow is None):
            return

        if n < self._atr_period + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument

        # -- Exit logic --
        if self._in_trade:
            current = _current_premium(
                self._entry_premium, self._entry_underlying,
                bar.close, self._direction,
            )
            bars_held = self._bar_count - self._entry_bar_idx
            should_exit = (
                current >= self._entry_premium * (1.0 + self._tp_pct)
                or current <= self._entry_premium * (1.0 - self._sl_pct)
                or bars_held >= self._max_hold
                or _is_expiry_cutoff(bar.ts)
            )
            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # -- Entry logic: EMA crossover + RSI confirmation --
        if self._trades_today >= self._max_trades:
            return

        cross_up = prev_fast <= prev_slow and self._ema_fast_val > self._ema_slow_val
        cross_down = (
            prev_fast >= prev_slow and self._ema_fast_val < self._ema_slow_val
        )

        if not cross_up and not cross_down:
            return

        rsi_val = _rsi(self._closes, self._rsi_period)
        if rsi_val is None:
            return

        premium = _PREMIUM_ATR_FRAC * atr

        if cross_up and rsi_val > self._rsi_long:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1
        elif cross_down and rsi_val < self._rsi_short:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1


# =====================================================================
# 4. RSI Extreme Mean Reversion
# =====================================================================

class RSIExtreme(Strategy):
    """RSI Mean Reversion on 1-minute options.

    Source: QuantifiedStrategies, 69% win rate cross-market.

    3-period RSI < 10 -> buy CE (extreme oversold snap-back).
    3-period RSI > 90 -> buy PE (extreme overbought fade).
    Exit when RSI crosses 70 (for CE) or 30 (for PE).
    SL: 20% of premium.  Max hold: 30 bars.
    Skip first 15 bars and last 60 bars of session.
    Max 3 trades per day.  Thursday 14:30 expiry cutoff.
    """

    name = "RSI Extreme Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        rsi_period: int = 3,
        rsi_entry_low: float = 10.0,
        rsi_entry_high: float = 90.0,
        rsi_exit_ce: float = 70.0,
        rsi_exit_pe: float = 30.0,
        sl_pct: float = 0.20,
        max_hold: int = 30,
        max_trades: int = 3,
        skip_first_bars: int = 15,
        skip_last_bars: int = 60,
        session_bars: int = 375,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._rsi_period = rsi_period
        self._rsi_entry_low = rsi_entry_low
        self._rsi_entry_high = rsi_entry_high
        self._rsi_exit_ce = rsi_exit_ce
        self._rsi_exit_pe = rsi_exit_pe
        self._sl_pct = sl_pct
        self._max_hold = max_hold
        self._max_trades = max_trades
        self._skip_first = skip_first_bars
        self._no_entry_after = session_bars - skip_last_bars

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._current_date = None
        self._session_bar_count = 0
        self._bar_count = 0
        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0
        self._entry_bar_idx = 0
        self._trades_today = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

        # -- Session tracking (reset on new date) --
        bar_date = bar.ts.date()
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._session_bar_count = 0
            self._trades_today = 0
            if self._in_trade:
                ctx.sell(bar.instrument, quantity=1)
                self._in_trade = False
                self._direction = 0

        self._session_bar_count += 1

        min_bars = max(self._atr_period + 1, self._rsi_period + 1)
        if len(self._closes) < min_bars:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        rsi_val = _rsi(self._closes, self._rsi_period)
        if rsi_val is None:
            return

        inst = bar.instrument

        # -- Exit logic --
        if self._in_trade:
            should_exit = False

            # RSI-based exit (primary target)
            if self._direction == 1 and rsi_val >= self._rsi_exit_ce:
                should_exit = True
            elif self._direction == -1 and rsi_val <= self._rsi_exit_pe:
                should_exit = True

            # Premium SL
            if not should_exit:
                current = _current_premium(
                    self._entry_premium, self._entry_underlying,
                    bar.close, self._direction,
                )
                if current <= self._entry_premium * (1.0 - self._sl_pct):
                    should_exit = True

            # Max hold
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold:
                should_exit = True

            if _is_expiry_cutoff(bar.ts):
                should_exit = True

            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # -- Entry logic --
        if self._trades_today >= self._max_trades:
            return

        # Skip first 15 bars of session (opening noise)
        if self._session_bar_count <= self._skip_first:
            return

        # Skip last 60 bars of session (closing noise / gamma risk)
        if self._session_bar_count > self._no_entry_after:
            return

        premium = _PREMIUM_ATR_FRAC * atr

        if rsi_val < self._rsi_entry_low:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1
        elif rsi_val > self._rsi_entry_high:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1


# =====================================================================
# 5. Bollinger Band Mean Reversion
# =====================================================================

class BBReversion(Strategy):
    """Bollinger Band Mean Reversion on 1-minute options.

    Source: Sahi.com Scalping Guide.

    20-period SMA + 2-SD Bollinger Bands on 1m closes.
    Only trade when bandwidth < 1.5% (sideways market filter).
    Price touches lower band -> buy CE (expect reversion to mean).
    Price touches upper band -> buy PE (expect reversion to mean).
    Target: price returns to 20-SMA (midline exit).
    SL: 15% of premium.  Max hold: 30 bars.
    Max 3 trades per day.  Thursday 14:30 expiry cutoff.
    """

    name = "BB Reversion Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        bb_period: int = 20,
        bb_std: float = 2.0,
        max_bandwidth_pct: float = 1.5,
        sl_pct: float = 0.15,
        max_hold: int = 30,
        max_trades: int = 3,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._max_bw = max_bandwidth_pct / 100.0  # 1.5% -> 0.015
        self._sl_pct = sl_pct
        self._max_hold = max_hold
        self._max_trades = max_trades

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._current_date = None
        self._bar_count = 0
        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0
        self._entry_bar_idx = 0
        self._trades_today = 0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

        # -- Daily reset --
        bar_date = bar.ts.date()
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._trades_today = 0
            if self._in_trade:
                ctx.sell(bar.instrument, quantity=1)
                self._in_trade = False
                self._direction = 0

        n = len(self._closes)
        min_bars = max(self._bb_period, self._atr_period + 1)
        if n < min_bars:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        # -- Compute Bollinger Bands --
        window = self._closes[-self._bb_period:]
        sma = sum(window) / self._bb_period
        if sma == 0:
            return

        variance = sum((c - sma) ** 2 for c in window) / self._bb_period
        sd = math.sqrt(variance)
        upper = sma + self._bb_std * sd
        lower = sma - self._bb_std * sd
        bandwidth = (upper - lower) / sma  # fractional, compare to self._max_bw

        inst = bar.instrument

        # -- Exit logic --
        if self._in_trade:
            should_exit = False

            # Midline target: exit when price returns to current SMA
            if self._direction == 1 and bar.close >= sma:
                should_exit = True
            elif self._direction == -1 and bar.close <= sma:
                should_exit = True

            # Premium SL
            if not should_exit:
                current = _current_premium(
                    self._entry_premium, self._entry_underlying,
                    bar.close, self._direction,
                )
                if current <= self._entry_premium * (1.0 - self._sl_pct):
                    should_exit = True

            # Max hold
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold:
                should_exit = True

            if _is_expiry_cutoff(bar.ts):
                should_exit = True

            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # -- Entry logic --
        if self._trades_today >= self._max_trades:
            return

        # Only trade in sideways market (narrow bandwidth)
        if bandwidth > self._max_bw:
            return

        premium = _PREMIUM_ATR_FRAC * atr

        if bar.close <= lower:
            # Lower band touch -> buy CE (expect reversion up to SMA)
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1
        elif bar.close >= upper:
            # Upper band touch -> buy PE (expect reversion down to SMA)
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close
            self._entry_bar_idx = self._bar_count
            self._trades_today += 1


# =====================================================================
# Register all research-backed options strategies
# =====================================================================

_register(
    "options_orb_research",
    "ORB Options",
    (
        "Opening Range Breakout on 1m options: build OR from first 30 bars "
        "(9:15-9:45), breakout above -> CE, below -> PE. "
        "SL: 20% premium, TP: 40% premium (2x risk). "
        "Time exit at bar 315 (~14:30). Max 1 trade/day. "
        "Sharpe 1.16, 48.7% win rate over 8-year backtest."
    ),
    lambda sym, **kw: ORBOptions(sym, **{
        k: v for k, v in kw.items()
        if k in ("atr_period", "or_bars", "sl_pct", "tp_pct", "exit_bar")
    }),
    source="IntradayLab 8-year BANKNIFTY ORB backtest. https://intradaylab.com",
)

_register(
    "options_vwap_pullback",
    "VWAP Pullback Options",
    (
        "VWAP Pullback Scalp on 1m options: price pulls back to within "
        "0.15% of session VWAP with reversal candle confirmation. "
        "CE above VWAP, PE below VWAP. "
        "SL: 15% premium, TP: ATR-scaled 15-point equivalent. "
        "Skip midday (bars 135-270). Max 3 trades/day. "
        "Claimed Sharpe 1.8, 68% win rate."
    ),
    lambda sym, **kw: VWAPPullback(sym, **{
        k: v for k, v in kw.items()
        if k in ("atr_period", "pullback_pct", "sl_pct", "tp_atr_frac",
                 "max_trades", "midday_start_bar", "midday_end_bar")
    }),
    source="OneTradeJournal VWAP pullback scalp. https://onetradejournal.com",
)

_register(
    "options_ema_crossover_scalp",
    "EMA Crossover Scalp Options",
    (
        "EMA 9/21 Crossover on 1m options: fast EMA crosses above slow -> CE, "
        "below -> PE. RSI(14) confirmation: >55 for CE, <45 for PE. "
        "SL: 15% premium, TP: 30% premium gain. "
        "Max hold 30 bars, max 5 trades/day."
    ),
    lambda sym, **kw: EMACrossoverScalp(sym, **{
        k: v for k, v in kw.items()
        if k in ("atr_period", "ema_fast", "ema_slow", "rsi_period",
                 "rsi_long", "rsi_short", "sl_pct", "tp_pct",
                 "max_hold", "max_trades")
    }),
    source="Tradejini Technical Playbook EMA crossover. https://tradejini.com",
)

_register(
    "options_rsi_extreme",
    "RSI Extreme Options",
    (
        "RSI(3) Mean Reversion on 1m options: RSI < 10 -> CE (oversold snap-back), "
        "RSI > 90 -> PE (overbought fade). "
        "Exit when RSI crosses 70 (CE) or 30 (PE). "
        "SL: 20% premium, max hold 30 bars. "
        "Skip first 15 and last 60 bars. Max 3 trades/day. "
        "69% win rate cross-market."
    ),
    lambda sym, **kw: RSIExtreme(sym, **{
        k: v for k, v in kw.items()
        if k in ("atr_period", "rsi_period", "rsi_entry_low", "rsi_entry_high",
                 "rsi_exit_ce", "rsi_exit_pe", "sl_pct", "max_hold",
                 "max_trades", "skip_first_bars", "skip_last_bars", "session_bars")
    }),
    source=(
        "QuantifiedStrategies RSI extreme mean reversion. "
        "https://quantifiedstrategies.com"
    ),
)

_register(
    "options_bb_reversion",
    "BB Reversion Options",
    (
        "Bollinger Band Mean Reversion on 1m options: 20-period SMA + 2-SD bands. "
        "Only trade when bandwidth < 1.5% (sideways). "
        "Lower band touch -> CE, upper band touch -> PE. "
        "Target: midline (20-SMA). SL: 15% premium. "
        "Max hold 30 bars, max 3 trades/day."
    ),
    lambda sym, **kw: BBReversion(sym, **{
        k: v for k, v in kw.items()
        if k in ("atr_period", "bb_period", "bb_std", "max_bandwidth_pct",
                 "sl_pct", "max_hold", "max_trades")
    }),
    source="Sahi.com scalping guide Bollinger Band reversion. https://sahi.com",
)
