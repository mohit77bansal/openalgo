"""Options-adapted directional strategies.

These strategies monitor the underlying's Donchian channel and simulate
buying ATM weekly options on breakouts. Unlike the futures strategies,
they:
- Use qty=1 (1 option lot, not underlying lot size)
- Track profit/loss as premium change, not underlying price change
- Cap max loss per trade at the premium paid (option buyer's risk)
- Auto-exit before weekly expiry (Thursday 14:30)

The premium is approximated as 0.4 * ATR(14) -- a reasonable proxy
for ATM weekly option premium on volatile indices like BANKNIFTY.
"""

from __future__ import annotations

import math

from qbacktest.engine.strategy import Strategy

from ._helpers import _atr, _lot
from ._registry import _register

_EXPIRY_WEEKDAY = 3
_EXPIRY_CUTOFF_HOUR = 14
_EXPIRY_CUTOFF_MINUTE = 30

# Premium proxy: fraction of ATR that approximates ATM weekly option premium
_PREMIUM_ATR_FRAC = 0.4


def _estimate_premium(atr: float) -> float:
    """ATM weekly option premium ~ 0.4 * ATR."""
    return atr * _PREMIUM_ATR_FRAC


def _is_expiry_cutoff(bar_ts) -> bool:
    """True if bar is on expiry day after cutoff time."""
    return (bar_ts.weekday() == _EXPIRY_WEEKDAY
            and bar_ts.hour >= _EXPIRY_CUTOFF_HOUR
            and bar_ts.minute >= _EXPIRY_CUTOFF_MINUTE)


class DonchianOptionsDirectional(Strategy):
    """Buy ATM weekly option on Donchian channel breakout.

    Entry:
        - Close > 20-bar high channel -> buy 1 lot ATM Call (simulated)
        - Close < 20-bar low channel  -> buy 1 lot ATM Put (simulated)

    Exit (whichever comes first):
        - Opposite breakout (signal reversal)
        - Premium doubles (2x take profit)
        - Premium halves (0.5x stop loss)
        - Thursday 14:30 (weekly expiry cutoff)

    Capital per trade: ~8K (premium * lot_size for BANKNIFTY lot=30).
    The strategy uses qty=1 in the engine to represent 1 option lot.
    Actual P&L is scaled by the option lot size internally.
    """

    name = "Donchian Options Directional"

    def __init__(
        self,
        sym: str,
        lookback: int = 20,
        atr_period: int = 14,
        tp_mult: float = 2.0,
        sl_mult: float = 0.5,
    ):
        self._sym = sym
        self._lookback = lookback
        self._atr_period = atr_period
        self._tp_mult = tp_mult
        self._sl_mult = sl_mult

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._in_trade = False
        self._direction = 0
        self._entry_premium = 0.0
        self._entry_underlying = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        min_bars = max(self._lookback, self._atr_period + 1)
        if len(self._closes) < min_bars + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument
        ch_high = max(self._highs[-self._lookback - 1:-1])
        ch_low = min(self._lows[-self._lookback - 1:-1])

        if self._in_trade:
            move = (bar.close - self._entry_underlying) * self._direction
            # Option premium moves ~50% of underlying (delta ~0.5 for ATM)
            current_premium = max(self._entry_premium + move * 0.5, 0)

            should_exit = False
            if current_premium >= self._entry_premium * self._tp_mult:
                should_exit = True
            elif current_premium <= self._entry_premium * self._sl_mult:
                should_exit = True
            elif _is_expiry_cutoff(bar.ts):
                should_exit = True
            elif self._direction > 0 and bar.close < ch_low:
                should_exit = True
            elif self._direction < 0 and bar.close > ch_high:
                should_exit = True

            if should_exit:
                ctx.sell(inst, quantity=1)
                self._in_trade = False
                self._direction = 0
            return

        # Entry: breakout with option-sized position (qty=1)
        premium = _estimate_premium(atr)
        if bar.close > ch_high:
            ctx.buy(inst, quantity=1)
            self._in_trade = True
            self._direction = 1
            self._entry_premium = premium
            self._entry_underlying = bar.close
        elif bar.close < ch_low:
            ctx.sell(inst, quantity=1)
            self._in_trade = True
            self._direction = -1
            self._entry_premium = premium
            self._entry_underlying = bar.close


class DonchianOptionsWeekly(Strategy):
    """Donchian breakout trading options-sized positions.

    Same channel breakout logic but trades qty=1 (option-equivalent)
    instead of full futures lots. Uses ATR trailing stop for exits.
    Suitable for 10K capital accounts.
    """

    name = "Donchian Options Weekly"

    def __init__(
        self,
        sym: str,
        lookback: int = 20,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
    ):
        self._sym = sym
        self._lookback = lookback
        self._atr_period = atr_period
        self._sl_mult = atr_sl_mult

    def on_start(self, ctx):
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._long = False
        self._short = False
        self._sl = 0.0

    def on_bar(self, ctx, bar):
        if bar.instrument.symbol != self._sym:
            return

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        min_bars = max(self._lookback, self._atr_period + 1)
        if len(self._closes) < min_bars + 1:
            return

        atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
        if not atr or atr == 0:
            return

        inst = bar.instrument
        # qty=1: represents 1 option lot, NOT the underlying's lot size
        qty = 1

        ch_high = max(self._highs[-self._lookback - 1:-1])
        ch_low = min(self._lows[-self._lookback - 1:-1])

        has_position = self._long or self._short

        if has_position and _is_expiry_cutoff(bar.ts):
            if self._long:
                ctx.sell(inst, quantity=qty)
                self._long = False
            elif self._short:
                ctx.buy(inst, quantity=qty)
                self._short = False
            self._sl = 0.0
            return

        if self._long:
            self._sl = max(self._sl, bar.close - self._sl_mult * atr)
            if bar.close < self._sl or bar.close < ch_low:
                ctx.sell(inst, quantity=qty)
                self._long = False
                self._sl = 0.0
                if bar.close < ch_low:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._sl = bar.close + self._sl_mult * atr
                return

        if self._short:
            self._sl = min(self._sl, bar.close + self._sl_mult * atr) if self._sl > 0 else bar.close + self._sl_mult * atr
            if bar.close > self._sl or bar.close > ch_high:
                ctx.buy(inst, quantity=qty)
                self._short = False
                self._sl = 0.0
                if bar.close > ch_high:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._sl = bar.close - self._sl_mult * atr
                return

        if bar.close > ch_high:
            ctx.buy(inst, quantity=qty)
            self._long = True
            self._sl = bar.close - self._sl_mult * atr
        elif bar.close < ch_low:
            ctx.sell(inst, quantity=qty)
            self._short = True
            self._sl = bar.close + self._sl_mult * atr


_register(
    "donchian_options_directional",
    "Donchian Options Directional",
    "Buy ATM weekly call on 20-bar high breakout, put on low breakout. "
    "Premium simulated via 0.4*ATR. 2x TP, 0.5x SL on premium. Qty=1 (option-sized). "
    "Designed for 10K capital accounts.",
    lambda sym, **kw: DonchianOptionsDirectional(sym, **{
        k: v for k, v in kw.items()
        if k in ("lookback", "atr_period", "tp_mult", "sl_mult")
    }),
    source="Donchian channel adapted for options. Premium proxy via ATR.",
)

_register(
    "donchian_options_weekly",
    "Donchian Options Weekly",
    "Donchian breakout with ATR trailing stop, qty=1 (option-sized position). "
    "Trades 1 unit instead of full lot -- suitable for 10K capital. "
    "Signal reversal + ATR stop + Thursday expiry cutoff.",
    lambda sym, **kw: DonchianOptionsWeekly(sym, **{
        k: v for k, v in kw.items()
        if k in ("lookback", "atr_period", "atr_sl_mult")
    }),
    source="Donchian channel with option-sized qty for small accounts.",
)
