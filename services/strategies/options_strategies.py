"""Options buying strategies for BANKNIFTY weekly options.

Six intraday strategies designed for option BUYING only (max loss = premium
paid).  All use the ATR-based premium proxy established in
``options_directional.py`` because the backtest engine is bar-based and has
no native options chain.

Capital constraint: 10K account, 8K per trade (1 lot = 30 units).
All positions are intraday -- no overnight holds.
"""

from __future__ import annotations

import math

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register

# -- Constants ----------------------------------------------------------------

_EXPIRY_WEEKDAY = 3          # Thursday
_MARKET_OPEN_HOUR = 9
_MARKET_OPEN_MINUTE = 15
_DEFAULT_CAPITAL = 8000.0
_PREMIUM_ATR_FRAC = 0.4      # ATM weekly premium ~ 0.4 * ATR(14)


# -- Shared helpers -----------------------------------------------------------

def _time_hm(ctx) -> tuple[int, int] | None:
    """Return (hour, minute) from the context clock, or None."""
    now = getattr(ctx, "now", None)
    if now is None:
        return None
    return (now.hour, now.minute)


def _is_expiry_day(ctx) -> bool:
    """True when the current bar falls on Thursday (weekly expiry)."""
    now = getattr(ctx, "now", None)
    return now is not None and now.weekday() == _EXPIRY_WEEKDAY


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average over the last *period* values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _vwap_proxy(closes: list[float], volumes: list[float], period: int) -> float | None:
    """Volume-weighted average price proxy.  Falls back to SMA when volumes
    are unavailable or all zero."""
    if len(closes) < period:
        return None
    c_slice = closes[-period:]
    v_slice = volumes[-period:] if len(volumes) >= period else []
    total_vol = sum(v_slice) if v_slice else 0.0
    if total_vol > 0:
        return sum(c * v for c, v in zip(c_slice, v_slice)) / total_vol
    return sum(c_slice) / period


def _estimate_premium(entry_premium: float, bar_close: float,
                      prev_close: float, is_call: bool) -> float:
    """Rough premium estimate based on underlying movement (delta ~0.5).

    Mirrors the approach in ``options_directional.py``.
    """
    if entry_premium == 0:
        return 0.0
    move = bar_close - prev_close
    delta_pnl = 0.5 * move if is_call else -0.5 * move
    raw = entry_premium + delta_pnl
    # Premium can't go below ~10% of entry (time value floor)
    return max(raw, entry_premium * 0.1)


# =====================================================================
# 1. Straddle Breakout
# =====================================================================

class StraddleBreakout(Strategy):
    """Buy both ATM CE and PE at 9:20 AM.

    Exit rules (whichever first):
      - One leg doubles -> sell both (winner pays for loser + profit)
      - Neither leg gains 50%+ by 14:00 -> exit both (time stop)
      - Thursday 14:30 expiry cutoff

    Works best on high-volatility days (budget, RBI policy, expiry).
    """

    name = "Straddle Breakout"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        winner_mult: float = 2.0,
        time_stop_min_gain: float = 0.5,
        time_stop_hour: int = 14,
        time_stop_minute: int = 0,
        entry_hour: int = 9,
        entry_minute: int = 20,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._winner_mult = winner_mult
        self._time_stop_min_gain = time_stop_min_gain
        self._time_stop = (time_stop_hour, time_stop_minute)
        self._entry_time = (entry_hour, entry_minute)
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_straddle = False
        self._entry_premium_ce = 0.0
        self._entry_premium_pe = 0.0
        self._current_premium_ce = 0.0
        self._current_premium_pe = 0.0
        self._entered_today = False

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        if len(self._closes) < self._atr_period + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        hm = _time_hm(ctx)
        inst = bar.instrument
        qty = 1  # 1 option lot (straddle = 2 legs but each is 1 lot)

        # -- Reset day flag at market open --
        if hm and hm <= (_MARKET_OPEN_HOUR, _MARKET_OPEN_MINUTE + 1):
            self._entered_today = False

        # -- Entry: 9:20 AM, buy both legs --
        if (
            not self._in_straddle
            and not self._entered_today
            and hm
            and hm >= self._entry_time
            and hm < (self._entry_time[0], self._entry_time[1] + 5)
        ):
            premium = self._premium_frac * atr
            # Buy two legs (simulated as one underlying position)
            ctx.buy(inst, quantity=qty)
            self._in_straddle = True
            self._entered_today = True
            self._entry_premium_ce = premium
            self._entry_premium_pe = premium
            self._current_premium_ce = premium
            self._current_premium_pe = premium
            return

        if not self._in_straddle:
            return

        # -- Update premium estimates --
        if len(self._closes) >= 2:
            prev = self._closes[-2]
            self._current_premium_ce = _estimate_premium(
                self._entry_premium_ce, bar.close, prev, is_call=True,
            )
            self._current_premium_pe = _estimate_premium(
                self._entry_premium_pe, bar.close, prev, is_call=False,
            )

        # -- Exit: Thursday 14:30 cutoff --
        if _is_expiry_day(ctx) and hm and hm >= (14, 30):
            self._close_straddle(ctx, inst, qty)
            return

        # -- Exit: one leg doubles --
        if (
            self._current_premium_ce >= self._entry_premium_ce * self._winner_mult
            or self._current_premium_pe >= self._entry_premium_pe * self._winner_mult
        ):
            self._close_straddle(ctx, inst, qty)
            return

        # -- Exit: time stop -- neither leg gained 50%+ by cutoff --
        if hm and hm >= self._time_stop:
            ce_gain = (self._current_premium_ce - self._entry_premium_ce) / self._entry_premium_ce
            pe_gain = (self._current_premium_pe - self._entry_premium_pe) / self._entry_premium_pe
            if ce_gain < self._time_stop_min_gain and pe_gain < self._time_stop_min_gain:
                self._close_straddle(ctx, inst, qty)
                return

    def _close_straddle(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_straddle = False
        self._entry_premium_ce = 0.0
        self._entry_premium_pe = 0.0
        self._current_premium_ce = 0.0
        self._current_premium_pe = 0.0


# =====================================================================
# 2. Momentum Scalp Options
# =====================================================================

class MomentumScalpOptions(Strategy):
    """Track 5-min momentum bursts on the underlying.

    Entry signal: 3 consecutive same-direction candles with increasing
    volume (approximated via range expansion when volume is unavailable).
      - Bullish burst  -> buy ATM CE
      - Bearish burst  -> buy ATM PE

    Exit: 30% profit target or 20% stop loss on premium.
    Max 3 trades per day.
    """

    name = "Momentum Scalp Options"

    def __init__(
        self,
        sym: str,
        burst_bars: int = 3,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        tp_pct: float = 0.30,
        sl_pct: float = 0.20,
        max_trades_per_day: int = 3,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._burst_bars = burst_bars
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_trades = max_trades_per_day
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._opens: list[float] = []
        self._volumes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._trades_today = 0
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._opens.append(getattr(bar, "open", bar.close))
        self._volumes.append(getattr(bar, "volume", 0.0))

        if len(self._closes) < self._atr_period + 1:
            return

        # -- Reset daily trade counter --
        now = getattr(ctx, "now", None)
        if now is not None:
            day_ord = now.toordinal() if hasattr(now, "toordinal") else None
            if day_ord and day_ord != self._last_reset_day:
                self._trades_today = 0
                self._last_reset_day = day_ord

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument
        qty = 1
        hm = _time_hm(ctx)

        # -- Exit logic (before entry) --
        if self._in_call or self._in_put:
            # Expiry cutoff
            if _is_expiry_day(ctx) and hm and hm >= (14, 30):
                self._close_position(ctx, inst, qty)
                return

            # EOD cutoff (3:00 PM)
            if hm and hm >= (15, 0):
                self._close_position(ctx, inst, qty)
                return

            # Premium-based TP/SL
            if len(self._closes) >= 2:
                current = _estimate_premium(
                    self._entry_premium, bar.close, self._closes[-2],
                    is_call=self._in_call,
                )
                if current >= self._tp_level:
                    self._close_position(ctx, inst, qty)
                    return
                if current <= self._sl_level:
                    self._close_position(ctx, inst, qty)
                    return
            return

        # -- Entry logic: detect momentum burst --
        if self._trades_today >= self._max_trades:
            return

        n = len(self._closes)
        if n < self._burst_bars + 1:
            return

        burst = self._burst_bars
        bullish_burst = True
        bearish_burst = True
        for i in range(-burst, 0):
            bar_close_i = self._closes[i]
            bar_open_i = self._opens[i]
            range_i = self._highs[i] - self._lows[i]
            prev_range = self._highs[i - 1] - self._lows[i - 1] if (n + i - 1) >= 0 else 0

            # Volume proxy: range expansion (wider bars = more activity)
            vol_i = self._volumes[i] if self._volumes[i] > 0 else range_i
            prev_vol = self._volumes[i - 1] if (self._volumes[i - 1] > 0 and (n + i - 1) >= 0) else prev_range

            if bar_close_i <= bar_open_i or vol_i < prev_vol:
                bullish_burst = False
            if bar_close_i >= bar_open_i or vol_i < prev_vol:
                bearish_burst = False

        if not bullish_burst and not bearish_burst:
            return

        premium = self._premium_frac * atr

        if bullish_burst:
            ctx.buy(inst, quantity=qty)
            self._in_call = True
            self._in_put = False
        else:
            ctx.buy(inst, quantity=qty)
            self._in_put = True
            self._in_call = False

        self._entry_premium = premium
        self._tp_level = premium * (1.0 + self._tp_pct)
        self._sl_level = premium * (1.0 - self._sl_pct)
        self._trades_today += 1

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0


# =====================================================================
# 3. ORB Options (Opening Range Breakout)
# =====================================================================

class ORBOptions(Strategy):
    """Opening Range Breakout applied to weekly options.

    Phase 1 (9:15-9:30): accumulate the 15-min opening range.
    Phase 2 (after 9:30):
      - Break above range high -> buy ATM CE
      - Break below range low  -> buy ATM PE
    Stop loss: opposite end of the range.
    Target: 1.5x the range width.
    Exit by 15:00 regardless.
    """

    name = "ORB Options"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        range_start: tuple[int, int] = (9, 15),
        range_end: tuple[int, int] = (9, 30),
        target_mult: float = 1.5,
        eod_hour: int = 15,
        eod_minute: int = 0,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._range_start = range_start
        self._range_end = range_end
        self._target_mult = target_mult
        self._eod = (eod_hour, eod_minute)
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        # Opening range state (reset daily)
        self._or_high = 0.0
        self._or_low = float("inf")
        self._or_built = False

        # Position state
        self._in_call = False
        self._in_put = False
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._traded_today = False
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        hm = _time_hm(ctx)
        if hm is None:
            return

        inst = bar.instrument
        qty = 1

        # -- Daily reset at open --
        now = getattr(ctx, "now", None)
        if now is not None:
            day_ord = now.toordinal() if hasattr(now, "toordinal") else None
            if day_ord and day_ord != self._last_reset_day:
                self._or_high = 0.0
                self._or_low = float("inf")
                self._or_built = False
                self._traded_today = False
                self._last_reset_day = day_ord

        # -- Phase 1: build opening range --
        if hm >= self._range_start and hm < self._range_end:
            self._or_high = max(self._or_high, bar.high)
            self._or_low = min(self._or_low, bar.low)
            return

        # Mark range as built once we pass the range window
        if not self._or_built and hm >= self._range_end:
            if self._or_high > 0 and self._or_low < float("inf"):
                self._or_built = True
            else:
                return

        if not self._or_built:
            return

        range_width = self._or_high - self._or_low
        if range_width <= 0:
            return

        # -- Exit logic --
        if self._in_call or self._in_put:
            # EOD exit
            if hm >= self._eod:
                self._close_position(ctx, inst, qty)
                return

            # Expiry cutoff
            if _is_expiry_day(ctx) and hm >= (14, 30):
                self._close_position(ctx, inst, qty)
                return

            # Stop loss (underlying-based)
            if self._in_call and bar.close <= self._sl_price:
                self._close_position(ctx, inst, qty)
                return
            if self._in_put and bar.close >= self._sl_price:
                self._close_position(ctx, inst, qty)
                return

            # Target (underlying-based)
            if self._in_call and bar.close >= self._tp_price:
                self._close_position(ctx, inst, qty)
                return
            if self._in_put and bar.close <= self._tp_price:
                self._close_position(ctx, inst, qty)
                return

            return

        # -- Entry logic (one trade per day) --
        if self._traded_today:
            return

        if bar.close > self._or_high:
            # Breakout above -> buy CE
            ctx.buy(inst, quantity=qty)
            self._in_call = True
            self._entry_price = bar.close
            self._sl_price = self._or_low          # opposite end of range
            self._tp_price = bar.close + self._target_mult * range_width
            self._traded_today = True

        elif bar.close < self._or_low:
            # Breakout below -> buy PE
            ctx.buy(inst, quantity=qty)
            self._in_put = True
            self._entry_price = bar.close
            self._sl_price = self._or_high          # opposite end of range
            self._tp_price = bar.close - self._target_mult * range_width
            self._traded_today = True

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0


# =====================================================================
# 4. Mean Reversion Options
# =====================================================================

class MeanReversionOptions(Strategy):
    """Buy reversal options when underlying is overextended from VWAP.

    Entry:
      - Underlying > 1.5% above VWAP (20-bar) -> buy ATM PE (reversal)
      - Underlying < 1.5% below VWAP (20-bar) -> buy ATM CE (reversal)

    Exit:
      - Target: 50% retracement back to VWAP
      - Stop: further 0.5% extension from entry
      - EOD / expiry cutoff
    """

    name = "Mean Reversion Options"

    def __init__(
        self,
        sym: str,
        vwap_period: int = 20,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        extension_pct: float = 1.5,
        stop_extension_pct: float = 0.5,
        retracement_pct: float = 0.50,
        eod_hour: int = 15,
        eod_minute: int = 0,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._vwap_period = vwap_period
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._ext_pct = extension_pct / 100.0
        self._stop_ext_pct = stop_extension_pct / 100.0
        self._retrace_pct = retracement_pct
        self._eod = (eod_hour, eod_minute)
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._volumes: list[float] = []

        self._in_call = False       # bought CE expecting move up (reversal from below)
        self._in_put = False        # bought PE expecting move down (reversal from above)
        self._entry_price = 0.0
        self._entry_vwap = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._volumes.append(getattr(bar, "volume", 0.0))

        if len(self._closes) < max(self._vwap_period, self._atr_period + 1):
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        vwap = _vwap_proxy(self._closes, self._volumes, self._vwap_period)
        if vwap is None or vwap == 0:
            return

        hm = _time_hm(ctx)
        inst = bar.instrument
        qty = 1

        # -- Exit logic --
        if self._in_call or self._in_put:
            # EOD exit
            if hm and hm >= self._eod:
                self._close_position(ctx, inst, qty)
                return

            # Expiry cutoff
            if _is_expiry_day(ctx) and hm and hm >= (14, 30):
                self._close_position(ctx, inst, qty)
                return

            # Stop loss
            if self._in_call and bar.close <= self._sl_price:
                self._close_position(ctx, inst, qty)
                return
            if self._in_put and bar.close >= self._sl_price:
                self._close_position(ctx, inst, qty)
                return

            # Target: 50% retracement toward VWAP
            if self._in_call and bar.close >= self._tp_price:
                self._close_position(ctx, inst, qty)
                return
            if self._in_put and bar.close <= self._tp_price:
                self._close_position(ctx, inst, qty)
                return

            return

        # -- Entry logic: overextension from VWAP --
        deviation_pct = (bar.close - vwap) / vwap

        if deviation_pct >= self._ext_pct:
            # Overextended above VWAP -> buy PE (expect reversal down)
            ctx.buy(inst, quantity=qty)
            self._in_put = True
            self._entry_price = bar.close
            self._entry_vwap = vwap
            self._sl_price = bar.close * (1.0 + self._stop_ext_pct)
            # Target: 50% of the way back to VWAP
            self._tp_price = bar.close - self._retrace_pct * (bar.close - vwap)

        elif deviation_pct <= -self._ext_pct:
            # Overextended below VWAP -> buy CE (expect reversal up)
            ctx.buy(inst, quantity=qty)
            self._in_call = True
            self._entry_price = bar.close
            self._entry_vwap = vwap
            self._sl_price = bar.close * (1.0 - self._stop_ext_pct)
            # Target: 50% of the way back to VWAP
            self._tp_price = bar.close + self._retrace_pct * (vwap - bar.close)

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_price = 0.0
        self._entry_vwap = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0


# =====================================================================
# 5. Expiry Day Theta Decay Fade (Lottery Ticket)
# =====================================================================

class ExpiryDayFade(Strategy):
    """Lottery-ticket strategy for weekly expiry day (Thursday only).

    At 11:00 AM, if the market has been range-bound (ATR of last ~24 bars
    < 0.5% of price), buy a deep-OTM option at very low premium hoping
    for a late-day breakout.

    Exit by 14:30 regardless.  Small cost, occasional big payoff.
    """

    name = "Expiry Day Theta Fade"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        range_atr_period: int = 24,
        range_threshold_pct: float = 0.5,
        premium_atr_frac: float = 0.15,      # deep OTM ~ 0.15 * ATR
        tp_mult: float = 5.0,                # 5x payoff target
        entry_hour: int = 11,
        entry_minute: int = 0,
        exit_hour: int = 14,
        exit_minute: int = 30,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._range_atr_period = range_atr_period
        self._range_thresh = range_threshold_pct / 100.0
        self._premium_frac = premium_atr_frac
        self._tp_mult = tp_mult
        self._entry_time = (entry_hour, entry_minute)
        self._exit_time = (exit_hour, exit_minute)
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_position = False
        self._is_call = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._entered_today = False
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        hm = _time_hm(ctx)
        if hm is None:
            return

        # Only trade on expiry day
        if not _is_expiry_day(ctx):
            return

        # Daily reset
        now = getattr(ctx, "now", None)
        if now is not None:
            day_ord = now.toordinal() if hasattr(now, "toordinal") else None
            if day_ord and day_ord != self._last_reset_day:
                self._entered_today = False
                self._last_reset_day = day_ord

        if len(self._closes) < max(self._atr_period + 1, self._range_atr_period + 1):
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument
        qty = 1

        # -- Exit logic --
        if self._in_position:
            # Time cutoff (14:30)
            if hm >= self._exit_time:
                self._close_position(ctx, inst, qty)
                return

            # Check for big move (TP)
            if len(self._closes) >= 2:
                current = _estimate_premium(
                    self._entry_premium, bar.close, self._closes[-2],
                    is_call=self._is_call,
                )
                if current >= self._tp_level:
                    self._close_position(ctx, inst, qty)
                    return
            return

        # -- Entry logic: 11:00 AM on expiry, range-bound check --
        if self._entered_today:
            return
        if hm < self._entry_time or hm >= (self._entry_time[0], self._entry_time[1] + 10):
            return

        # Range-bound check: recent ATR < threshold % of price
        range_atr = _atr(
            self._highs, self._lows, self._closes,
            min(self._range_atr_period, len(self._closes) - 1),
        )
        if range_atr is None or bar.close == 0:
            return
        if range_atr / bar.close >= self._range_thresh:
            return  # Too volatile -- not range-bound

        # Buy deep OTM (direction: slight bias from recent trend via SMA)
        sma = _sma(self._closes, 20)
        if sma is not None and bar.close > sma:
            # Slight bullish bias -> buy OTM CE
            self._is_call = True
        else:
            # Slight bearish bias -> buy OTM PE
            self._is_call = False

        premium = self._premium_frac * atr  # deep OTM is cheap
        ctx.buy(inst, quantity=qty)
        self._in_position = True
        self._entered_today = True
        self._entry_premium = premium
        self._tp_level = premium * self._tp_mult

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_position = False
        self._is_call = False
        self._entry_premium = 0.0
        self._tp_level = 0.0


# =====================================================================
# 6. IV Crush Play
# =====================================================================

class IVCrushPlay(Strategy):
    """Buy options directionally when IV is elevated before known events.

    Event heuristic: Tuesday before Thursday expiry (2 days before) tends
    to see elevated IV in weekly options.

    Entry condition:
      - ATR > 1.2x its own 20-day average (proxy for elevated IV)
      - Direction: 20-bar SMA trend (CE if close > SMA, PE if below)

    Exit:
      - 40% profit on premium (riding the directional move)
      - 25% stop loss on premium
      - EOD / expiry cutoff
    """

    name = "IV Crush Play"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        atr_avg_period: int = 20,
        iv_threshold: float = 1.2,
        sma_period: int = 20,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        tp_pct: float = 0.40,
        sl_pct: float = 0.25,
        event_weekday: int = 1,       # Tuesday = 1 (day-of-week heuristic)
        eod_hour: int = 15,
        eod_minute: int = 0,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._atr_avg_period = atr_avg_period
        self._iv_threshold = iv_threshold
        self._sma_period = sma_period
        self._premium_frac = premium_atr_frac
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._event_weekday = event_weekday
        self._eod = (eod_hour, eod_minute)
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._atr_history: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._traded_today = False
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        if len(self._closes) < max(self._atr_period + 1, self._sma_period):
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        self._atr_history.append(atr)

        hm = _time_hm(ctx)
        inst = bar.instrument
        qty = 1

        # Daily reset
        now = getattr(ctx, "now", None)
        if now is not None:
            day_ord = now.toordinal() if hasattr(now, "toordinal") else None
            if day_ord and day_ord != self._last_reset_day:
                self._traded_today = False
                self._last_reset_day = day_ord

        # -- Exit logic --
        if self._in_call or self._in_put:
            # EOD exit
            if hm and hm >= self._eod:
                self._close_position(ctx, inst, qty)
                return

            # Expiry cutoff
            if _is_expiry_day(ctx) and hm and hm >= (14, 30):
                self._close_position(ctx, inst, qty)
                return

            # Premium-based TP/SL
            if len(self._closes) >= 2:
                current = _estimate_premium(
                    self._entry_premium, bar.close, self._closes[-2],
                    is_call=self._in_call,
                )
                if current >= self._tp_level:
                    self._close_position(ctx, inst, qty)
                    return
                if current <= self._sl_level:
                    self._close_position(ctx, inst, qty)
                    return
            return

        # -- Entry logic: event day + elevated IV + directional --
        if self._traded_today:
            return

        # Event day heuristic (default: Tuesday before Thursday expiry)
        if now is not None and now.weekday() != self._event_weekday:
            return

        # IV proxy: current ATR vs its own moving average
        if len(self._atr_history) < self._atr_avg_period:
            return
        avg_atr = sum(self._atr_history[-self._atr_avg_period:]) / self._atr_avg_period
        if avg_atr == 0 or atr / avg_atr < self._iv_threshold:
            return  # IV not elevated enough

        # Direction from SMA
        sma = _sma(self._closes, self._sma_period)
        if sma is None:
            return

        premium = self._premium_frac * atr

        if bar.close > sma:
            # Bullish trend -> buy CE to ride directional move
            ctx.buy(inst, quantity=qty)
            self._in_call = True
        else:
            # Bearish trend -> buy PE
            ctx.buy(inst, quantity=qty)
            self._in_put = True

        self._entry_premium = premium
        self._tp_level = premium * (1.0 + self._tp_pct)
        self._sl_level = premium * (1.0 - self._sl_pct)
        self._traded_today = True

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0


# =====================================================================
# Register all options buying strategies
# =====================================================================

_register(
    "options_straddle_breakout",
    "Straddle Breakout",
    (
        "Buy ATM CE + PE at 9:20 AM. When one leg doubles, sell both "
        "(winner pays for loser + profit). Time stop: exit both if neither "
        "leg gains 50%+ by 2 PM. Best on high-vol days (budget, RBI, expiry). "
        "Option BUYING only. Max loss = combined premium."
    ),
    lambda sym, **kw: StraddleBreakout(
        sym,
        atr_period=kw.get("atr_period", 14),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        winner_mult=kw.get("winner_mult", 2.0),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Classic straddle breakout for event-driven vol. "
        "Adapted for weekly BANKNIFTY options with ATR premium proxy."
    ),
)

_register(
    "options_momentum_scalp",
    "Momentum Scalp Options",
    (
        "Track 5-min momentum burst (3 consecutive same-direction candles with "
        "increasing volume). Buy ATM CE on bullish burst, PE on bearish. "
        "30% TP / 20% SL on premium. Max 3 trades/day. "
        "Option BUYING only. Max loss = premium paid per trade."
    ),
    lambda sym, **kw: MomentumScalpOptions(
        sym,
        burst_bars=kw.get("burst_bars", 3),
        atr_period=kw.get("atr_period", 14),
        tp_pct=kw.get("tp_pct", 0.30),
        sl_pct=kw.get("sl_pct", 0.20),
        max_trades_per_day=kw.get("max_trades_per_day", 3),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Momentum scalping adapted for options. "
        "Volume-confirmed burst detection with quick TP/SL on premium."
    ),
)

_register(
    "options_orb",
    "ORB Options",
    (
        "Opening Range Breakout (9:15-9:30). Buy ATM CE on breakout above "
        "range high, PE on breakout below range low. SL at opposite end of "
        "range. Target 1.5x range width. Exit by 3 PM. "
        "Option BUYING only. One trade per day."
    ),
    lambda sym, **kw: ORBOptions(
        sym,
        atr_period=kw.get("atr_period", 14),
        target_mult=kw.get("target_mult", 1.5),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Opening Range Breakout (Toby Crabel, 1990). "
        "Adapted for BANKNIFTY weekly options with intraday exits."
    ),
)

_register(
    "options_mean_reversion",
    "Mean Reversion Options",
    (
        "Buy reversal options when underlying is 1.5%+ from 20-bar VWAP. "
        "CE if below VWAP (expect bounce), PE if above (expect pullback). "
        "Target: 50% retracement to VWAP. Stop: further 0.5% extension. "
        "Option BUYING only. Works in mean-reverting regimes."
    ),
    lambda sym, **kw: MeanReversionOptions(
        sym,
        vwap_period=kw.get("vwap_period", 20),
        atr_period=kw.get("atr_period", 14),
        extension_pct=kw.get("extension_pct", 1.5),
        stop_extension_pct=kw.get("stop_extension_pct", 0.5),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "VWAP mean reversion adapted for options. "
        "Buy reversal direction at overextended levels."
    ),
)

_register(
    "options_expiry_fade",
    "Expiry Day Theta Fade",
    (
        "Thursday-only lottery ticket. At 11 AM, if market is range-bound "
        "(ATR < 0.5% of price), buy deep OTM option at very low premium. "
        "If no breakout by 2:30 PM, exit. Occasional big payoff, small cost. "
        "Option BUYING only. Max loss = cheap premium."
    ),
    lambda sym, **kw: ExpiryDayFade(
        sym,
        atr_period=kw.get("atr_period", 14),
        range_threshold_pct=kw.get("range_threshold_pct", 0.5),
        tp_mult=kw.get("tp_mult", 5.0),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Expiry-day theta decay fade. "
        "Cheap deep-OTM lottery tickets on range-bound expiry days."
    ),
)

_register(
    "options_iv_crush",
    "IV Crush Play",
    (
        "Buy options directionally when IV is elevated (ATR > 1.2x its "
        "20-day average) on pre-event days (Tuesday heuristic). "
        "Direction from 20-bar SMA. 40% TP / 25% SL on premium. "
        "Option BUYING only. Rides directional move on vol expansion."
    ),
    lambda sym, **kw: IVCrushPlay(
        sym,
        atr_period=kw.get("atr_period", 14),
        iv_threshold=kw.get("iv_threshold", 1.2),
        sma_period=kw.get("sma_period", 20),
        tp_pct=kw.get("tp_pct", 0.40),
        sl_pct=kw.get("sl_pct", 0.25),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Pre-event IV expansion play. "
        "ATR-based IV proxy with SMA directional filter."
    ),
)
