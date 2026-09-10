"""Ultra-short-term options scalping strategies for BANKNIFTY weekly options.

Five intraday scalping strategies designed for option BUYING only (max loss =
premium paid).  All target 1-5 minute holds with tight TP/SL on premium.
Uses the ATR-based premium proxy established in ``options_strategies.py``
because the backtest engine is bar-based and has no native options chain.

Capital constraint: 10K account, 8K per trade (1 lot).
All positions are intraday -- no overnight holds.
"""

from __future__ import annotations

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


def _estimate_premium(entry_premium: float, bar_close: float,
                      prev_close: float, is_call: bool) -> float:
    """Rough premium estimate based on underlying movement (delta ~0.5)."""
    if entry_premium == 0:
        return 0.0
    move = bar_close - prev_close
    delta_pnl = 0.5 * move if is_call else -0.5 * move
    raw = entry_premium + delta_pnl
    return max(raw, entry_premium * 0.1)


def _rsi(closes: list[float], period: int) -> float | None:
    """Wilder's RSI over the last *period* + 1 closes.

    Uses simple average for the initial calculation (first window).
    Returns None when insufficient data.
    """
    if len(closes) < period + 1:
        return None

    # Compute changes over the last (period) bars
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


# =====================================================================
# 1. Scalp Momentum
# =====================================================================

class ScalpMomentum(Strategy):
    """2-bar momentum scalp on 1-min candles.

    Signal: 2 consecutive candles in the same direction, each moving > 0.15%
    of the previous close.
      - Bullish pair -> buy ATM CE
      - Bearish pair -> buy ATM PE

    Exit (whichever first):
      - 10% TP on premium
      - 7% SL on premium
      - 5-bar max hold time
      - Thursday 14:30 expiry cutoff
      - Max 5 trades per day
    """

    name = "Scalp Momentum"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        move_pct: float = 0.15,
        tp_pct: float = 0.10,
        sl_pct: float = 0.07,
        max_hold_bars: int = 5,
        max_trades_per_day: int = 5,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._move_pct = move_pct / 100.0
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_hold_bars = max_hold_bars
        self._max_trades = max_trades_per_day
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0
        self._bar_count = 0
        self._trades_today = 0
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

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

            # Max hold time
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold_bars:
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

        # -- Entry logic: 2 consecutive same-direction candles > 0.15% --
        if self._trades_today >= self._max_trades:
            return

        n = len(self._closes)
        if n < 3:
            return

        # Check last 2 candles
        move_1 = (self._closes[-2] - self._closes[-3]) / self._closes[-3] if self._closes[-3] != 0 else 0.0
        move_2 = (self._closes[-1] - self._closes[-2]) / self._closes[-2] if self._closes[-2] != 0 else 0.0

        bullish = move_1 > self._move_pct and move_2 > self._move_pct
        bearish = move_1 < -self._move_pct and move_2 < -self._move_pct

        if not bullish and not bearish:
            return

        premium = self._premium_frac * atr

        if bullish:
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
        self._entry_bar_idx = self._bar_count
        self._trades_today += 1

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0


# =====================================================================
# 2. Scalp Spread
# =====================================================================

class ScalpSpread(Strategy):
    """Volume-confirmed single-candle scalp.

    Signal: single 1-min candle moves > 0.1% with above-average volume
    (current bar volume > average of last 20 bars; falls back to range
    expansion when volume is unavailable).
      - Bullish candle -> buy ATM CE
      - Bearish candle -> buy ATM PE

    Exit (whichever first):
      - 8% TP on premium
      - 5% SL on premium
      - 3-bar max hold time
      - Thursday 14:30 expiry cutoff
      - Max 8 trades per day
    """

    name = "Scalp Spread"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        move_pct: float = 0.10,
        vol_lookback: int = 20,
        tp_pct: float = 0.08,
        sl_pct: float = 0.05,
        max_hold_bars: int = 3,
        max_trades_per_day: int = 8,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._move_pct = move_pct / 100.0
        self._vol_lookback = vol_lookback
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_hold_bars = max_hold_bars
        self._max_trades = max_trades_per_day
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._volumes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0
        self._bar_count = 0
        self._trades_today = 0
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._volumes.append(getattr(bar, "volume", 0.0))
        self._bar_count += 1

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

            # Max hold time
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold_bars:
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

        # -- Entry logic: single candle > 0.1% with above-average volume --
        if self._trades_today >= self._max_trades:
            return

        n = len(self._closes)
        if n < 2:
            return

        prev_close = self._closes[-2]
        if prev_close == 0:
            return

        candle_move = (bar.close - prev_close) / prev_close

        if abs(candle_move) <= self._move_pct:
            return

        # Volume confirmation: compare current volume/range to average
        has_volume = self._volumes[-1] > 0
        if has_volume and n > self._vol_lookback:
            avg_vol = sum(self._volumes[-self._vol_lookback - 1:-1]) / self._vol_lookback
            if avg_vol > 0 and self._volumes[-1] <= avg_vol:
                return
        elif n > self._vol_lookback:
            # Range expansion fallback
            current_range = self._highs[-1] - self._lows[-1]
            ranges = [
                self._highs[i] - self._lows[i]
                for i in range(-self._vol_lookback - 1, -1)
            ]
            avg_range = sum(ranges) / len(ranges) if ranges else 0.0
            if avg_range > 0 and current_range <= avg_range:
                return

        premium = self._premium_frac * atr

        if candle_move > 0:
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
        self._entry_bar_idx = self._bar_count
        self._trades_today += 1

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0


# =====================================================================
# 3. Scalp Reversal
# =====================================================================

class ScalpReversal(Strategy):
    """Counter-trend reversal scalp.

    Signal: 3 or more consecutive same-direction 1-min candles -> buy the
    OPPOSITE direction ATM option (fading the move).
      - 3+ bullish candles -> buy ATM PE (expect pullback)
      - 3+ bearish candles -> buy ATM CE (expect bounce)

    Exit (whichever first):
      - 12% TP on premium
      - 8% SL on premium
      - 5-bar max hold time
      - Thursday 14:30 expiry cutoff
      - Max 4 trades per day
    """

    name = "Scalp Reversal"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        streak_bars: int = 3,
        tp_pct: float = 0.12,
        sl_pct: float = 0.08,
        max_hold_bars: int = 5,
        max_trades_per_day: int = 4,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._streak_bars = streak_bars
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_hold_bars = max_hold_bars
        self._max_trades = max_trades_per_day
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0
        self._bar_count = 0
        self._trades_today = 0
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

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

            # Max hold time
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold_bars:
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

        # -- Entry logic: 3+ consecutive same-direction candles -> fade --
        if self._trades_today >= self._max_trades:
            return

        n = len(self._closes)
        if n < self._streak_bars + 1:
            return

        # Count consecutive same-direction candles from the latest bar backwards
        bullish_streak = 0
        bearish_streak = 0
        for i in range(1, self._streak_bars + 1):
            idx = n - i
            prev_idx = idx - 1
            if self._closes[idx] > self._closes[prev_idx]:
                bullish_streak += 1
            elif self._closes[idx] < self._closes[prev_idx]:
                bearish_streak += 1

        premium = self._premium_frac * atr

        if bullish_streak >= self._streak_bars:
            # 3+ up candles -> fade: buy PE expecting pullback
            ctx.buy(inst, quantity=qty)
            self._in_put = True
            self._in_call = False
        elif bearish_streak >= self._streak_bars:
            # 3+ down candles -> fade: buy CE expecting bounce
            ctx.buy(inst, quantity=qty)
            self._in_call = True
            self._in_put = False
        else:
            return

        self._entry_premium = premium
        self._tp_level = premium * (1.0 + self._tp_pct)
        self._sl_level = premium * (1.0 - self._sl_pct)
        self._entry_bar_idx = self._bar_count
        self._trades_today += 1

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0


# =====================================================================
# 4. Scalp Opening
# =====================================================================

class ScalpOpening(Strategy):
    """First-candle-of-day scalp in the opening 15 minutes (9:15-9:30).

    Signal: first candle after open with body > 0.1% of close.
      - Bullish first candle -> buy ATM CE
      - Bearish first candle -> buy ATM PE

    Exit (whichever first):
      - 15% TP on premium
      - 10% SL on premium
      - 10-bar max hold time
      - Thursday 14:30 expiry cutoff
      - Max 1 trade per day
    """

    name = "Scalp Opening"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        body_pct: float = 0.10,
        tp_pct: float = 0.15,
        sl_pct: float = 0.10,
        max_hold_bars: int = 10,
        window_start: tuple[int, int] = (9, 15),
        window_end: tuple[int, int] = (9, 30),
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._premium_frac = premium_atr_frac
        self._body_pct = body_pct / 100.0
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_hold_bars = max_hold_bars
        self._window_start = window_start
        self._window_end = window_end
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0
        self._bar_count = 0
        self._traded_today = False
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

        if len(self._closes) < self._atr_period + 1:
            return

        # -- Reset daily flag --
        now = getattr(ctx, "now", None)
        if now is not None:
            day_ord = now.toordinal() if hasattr(now, "toordinal") else None
            if day_ord and day_ord != self._last_reset_day:
                self._traded_today = False
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

            # Max hold time
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold_bars:
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

        # -- Entry logic: first candle in 9:15-9:30 window with body > 0.1% --
        if self._traded_today:
            return

        if hm is None:
            return

        if hm < self._window_start or hm >= self._window_end:
            return

        n = len(self._closes)
        if n < 2:
            return

        bar_open = getattr(bar, "open", self._closes[-2])
        if bar.close == 0:
            return

        body_move = (bar.close - bar_open) / bar.close

        if abs(body_move) <= self._body_pct:
            return

        premium = self._premium_frac * atr

        if body_move > 0:
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
        self._entry_bar_idx = self._bar_count
        self._traded_today = True

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0


# =====================================================================
# 5. Scalp Gamma (Expiry Day Only)
# =====================================================================

class ScalpGamma(Strategy):
    """Expiry-day gamma scalp using 5-period RSI on the underlying.

    Only trades on Thursday (weekly expiry). Uses extreme RSI readings
    to catch short-term mean-reversion moves amplified by gamma.
      - RSI < 25 -> buy ATM CE (oversold bounce)
      - RSI > 75 -> buy ATM PE (overbought fade)

    Exit (whichever first):
      - 20% TP on premium
      - 10% SL on premium
      - 5-bar max hold time
      - Thursday 14:30 expiry cutoff
      - Max 3 trades per day
    """

    name = "Scalp Gamma"

    def __init__(
        self,
        sym: str,
        atr_period: int = 14,
        rsi_period: int = 5,
        premium_atr_frac: float = _PREMIUM_ATR_FRAC,
        rsi_oversold: float = 25.0,
        rsi_overbought: float = 75.0,
        tp_pct: float = 0.20,
        sl_pct: float = 0.10,
        max_hold_bars: int = 5,
        max_trades_per_day: int = 3,
        capital_per_trade: float = _DEFAULT_CAPITAL,
    ):
        self._sym = sym
        self._atr_period = atr_period
        self._rsi_period = rsi_period
        self._premium_frac = premium_atr_frac
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._max_hold_bars = max_hold_bars
        self._max_trades = max_trades_per_day
        self._capital = capital_per_trade

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0
        self._bar_count = 0
        self._trades_today = 0
        self._last_reset_day: int | None = None

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._bar_count += 1

        # Only trade on expiry day
        if not _is_expiry_day(ctx):
            return

        if len(self._closes) < max(self._atr_period + 1, self._rsi_period + 1):
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
            if hm and hm >= (14, 30):
                self._close_position(ctx, inst, qty)
                return

            # Max hold time
            bars_held = self._bar_count - self._entry_bar_idx
            if bars_held >= self._max_hold_bars:
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

        # -- Entry logic: RSI extremes on expiry day --
        if self._trades_today >= self._max_trades:
            return

        rsi_val = _rsi(self._closes, self._rsi_period)
        if rsi_val is None:
            return

        premium = self._premium_frac * atr

        if rsi_val < self._rsi_oversold:
            # Oversold -> buy CE (expect bounce)
            ctx.buy(inst, quantity=qty)
            self._in_call = True
            self._in_put = False
        elif rsi_val > self._rsi_overbought:
            # Overbought -> buy PE (expect fade)
            ctx.buy(inst, quantity=qty)
            self._in_put = True
            self._in_call = False
        else:
            return

        self._entry_premium = premium
        self._tp_level = premium * (1.0 + self._tp_pct)
        self._sl_level = premium * (1.0 - self._sl_pct)
        self._entry_bar_idx = self._bar_count
        self._trades_today += 1

    def _close_position(self, ctx, inst, qty: int) -> None:
        ctx.sell(inst, quantity=qty)
        self._in_call = False
        self._in_put = False
        self._entry_premium = 0.0
        self._tp_level = 0.0
        self._sl_level = 0.0
        self._entry_bar_idx = 0


# =====================================================================
# Register all options scalping strategies
# =====================================================================

_register(
    "options_scalp_momentum",
    "Scalp Momentum",
    (
        "2-bar momentum scalp: 2 consecutive 1-min candles same direction, "
        "each > 0.15% move. Buy ATM CE on bullish pair, PE on bearish. "
        "10% TP / 7% SL on premium, 5-bar max hold. Max 5 trades/day. "
        "Option BUYING only. Ultra-short hold (1-5 min)."
    ),
    lambda sym, **kw: ScalpMomentum(
        sym,
        atr_period=kw.get("atr_period", 14),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        move_pct=kw.get("move_pct", 0.15),
        tp_pct=kw.get("tp_pct", 0.10),
        sl_pct=kw.get("sl_pct", 0.07),
        max_hold_bars=kw.get("max_hold_bars", 5),
        max_trades_per_day=kw.get("max_trades_per_day", 5),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Momentum continuation scalp for weekly options. "
        "Tight TP/SL with bar-count time stop."
    ),
)

_register(
    "options_scalp_spread",
    "Scalp Spread",
    (
        "Volume-confirmed single-candle scalp: 1-min candle > 0.1% move "
        "with above-average volume (or range expansion fallback). "
        "Buy ATM CE on bullish candle, PE on bearish. "
        "8% TP / 5% SL on premium, 3-bar max hold. Max 8 trades/day. "
        "Option BUYING only. Ultra-short hold (1-3 min)."
    ),
    lambda sym, **kw: ScalpSpread(
        sym,
        atr_period=kw.get("atr_period", 14),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        move_pct=kw.get("move_pct", 0.10),
        vol_lookback=kw.get("vol_lookback", 20),
        tp_pct=kw.get("tp_pct", 0.08),
        sl_pct=kw.get("sl_pct", 0.05),
        max_hold_bars=kw.get("max_hold_bars", 3),
        max_trades_per_day=kw.get("max_trades_per_day", 8),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Volume-confirmed spread scalp for weekly options. "
        "Tightest TP/SL in the scalping suite."
    ),
)

_register(
    "options_scalp_reversal",
    "Scalp Reversal",
    (
        "Counter-trend reversal scalp: 3+ consecutive same-direction 1-min "
        "candles -> buy OPPOSITE direction ATM option. "
        "12% TP / 8% SL on premium, 5-bar max hold. Max 4 trades/day. "
        "Option BUYING only. Fades short-term exhaustion moves."
    ),
    lambda sym, **kw: ScalpReversal(
        sym,
        atr_period=kw.get("atr_period", 14),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        streak_bars=kw.get("streak_bars", 3),
        tp_pct=kw.get("tp_pct", 0.12),
        sl_pct=kw.get("sl_pct", 0.08),
        max_hold_bars=kw.get("max_hold_bars", 5),
        max_trades_per_day=kw.get("max_trades_per_day", 4),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Mean-reversion scalp for weekly options. "
        "Fades exhaustion streaks with tight exits."
    ),
)

_register(
    "options_scalp_opening",
    "Scalp Opening",
    (
        "First 15 minutes scalp (9:15-9:30 only). Buy ATM CE/PE based on "
        "first candle direction if body > 0.1%. "
        "15% TP / 10% SL on premium, 10-bar max hold. Max 1 trade/day. "
        "Option BUYING only. Captures opening volatility burst."
    ),
    lambda sym, **kw: ScalpOpening(
        sym,
        atr_period=kw.get("atr_period", 14),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        body_pct=kw.get("body_pct", 0.10),
        tp_pct=kw.get("tp_pct", 0.15),
        sl_pct=kw.get("sl_pct", 0.10),
        max_hold_bars=kw.get("max_hold_bars", 10),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Opening auction volatility scalp. "
        "Trades the first significant candle of the session."
    ),
)

_register(
    "options_scalp_gamma",
    "Scalp Gamma",
    (
        "Expiry-day gamma scalp (Thursday only). Uses 5-period RSI on "
        "underlying: RSI < 25 -> buy CE, RSI > 75 -> buy PE. "
        "20% TP / 10% SL on premium, 5-bar max hold. Max 3 trades/day. "
        "Option BUYING only. Exploits amplified gamma on expiry."
    ),
    lambda sym, **kw: ScalpGamma(
        sym,
        atr_period=kw.get("atr_period", 14),
        rsi_period=kw.get("rsi_period", 5),
        premium_atr_frac=kw.get("premium_atr_frac", _PREMIUM_ATR_FRAC),
        rsi_oversold=kw.get("rsi_oversold", 25.0),
        rsi_overbought=kw.get("rsi_overbought", 75.0),
        tp_pct=kw.get("tp_pct", 0.20),
        sl_pct=kw.get("sl_pct", 0.10),
        max_hold_bars=kw.get("max_hold_bars", 5),
        max_trades_per_day=kw.get("max_trades_per_day", 3),
        capital_per_trade=kw.get("capital_per_trade", _DEFAULT_CAPITAL),
    ),
    source=(
        "Expiry-day gamma scalp. "
        "RSI extremes on Thursday with amplified option gamma."
    ),
)
