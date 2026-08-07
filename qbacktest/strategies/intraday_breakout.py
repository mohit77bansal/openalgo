"""Intraday Opening-Range breakout — equity strategy.

Strategy:
    1. From 09:15 to `or_minutes`, observe high/low ("OR high/low").
    2. If price breaks above OR high in the next `entry_window_min` mins, BUY.
    3. If price breaks below OR low, SELL (short, intraday).
    4. Stop loss = other side of OR. Take profit = OR_range × `tp_multiplier`.
    5. Square off at 15:15 IST.

Useful as a baseline equity intraday strategy.
"""

from __future__ import annotations

from datetime import time
from typing import ClassVar

from qbacktest.core.types import Bar, Side
from qbacktest.engine.context import Context
from qbacktest.engine.strategy import Strategy


class IntradayORBreakout(Strategy):
    name: ClassVar[str] = "IntradayORBreakout"

    params: ClassVar[dict] = {
        "symbol": "RELIANCE",
        "or_minutes": 15,
        "entry_window_min": 60,
        "exit_hour": 15,
        "exit_minute": 15,
        "tp_multiplier": 2.0,        # take profit = range × this
        "sl_multiplier": 1.0,        # stop loss = other side of OR
        "shares": 100,
    }

    def on_start(self, ctx: Context) -> None:
        self._reset()

    def on_session_open(self, ctx: Context) -> None:
        self._reset()

    def _reset(self) -> None:
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_locked = False
        self.position_taken = False
        self.entry_price: float | None = None
        self.tp_price: float | None = None
        self.sl_price: float | None = None
        self.direction: Side | None = None

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if bar.instrument.symbol.upper() != self.params["symbol"].upper():
            return

        # Lock OR after window
        cur_time = ctx.now.time()
        or_lock_time = time(9, 15 + self.params["or_minutes"])
        if not self.or_locked:
            if cur_time < or_lock_time:
                self.or_high = max(self.or_high or bar.high, bar.high)
                self.or_low = min(self.or_low or bar.low, bar.low)
                return
            else:
                self.or_locked = True
                return

        # Forced exit
        exit_t = time(self.params["exit_hour"], self.params["exit_minute"])
        if cur_time >= exit_t:
            ctx.square_off_all()
            return

        # Already in position — check tp/sl
        if self.position_taken:
            if self.direction == Side.BUY:
                if bar.high >= (self.tp_price or 0) or bar.low <= (self.sl_price or 0):
                    ctx.square_off_all()
                    self.position_taken = False
            else:
                if bar.low <= (self.tp_price or 0) or bar.high >= (self.sl_price or 0):
                    ctx.square_off_all()
                    self.position_taken = False
            return

        # Look for breakout in entry window
        entry_window_end = time(9, 15 + self.params["or_minutes"] + self.params["entry_window_min"])
        if cur_time > entry_window_end:
            return  # too late to enter

        sym = self.params["symbol"].upper()
        inst = ctx.equity_(sym)
        if self.or_high is not None and bar.high > self.or_high:
            self.entry_price = self.or_high
            self.direction = Side.BUY
            range_ = (self.or_high - (self.or_low or self.or_high))
            self.tp_price = self.or_high + range_ * self.params["tp_multiplier"]
            self.sl_price = self.or_low
            ctx.buy(inst, quantity=self.params["shares"], tag="orb.long")
            self.position_taken = True
            ctx.log("orb_long_entry", price=self.entry_price)
        elif self.or_low is not None and bar.low < self.or_low:
            self.entry_price = self.or_low
            self.direction = Side.SELL
            range_ = ((self.or_high or self.or_low) - self.or_low)
            self.tp_price = self.or_low - range_ * self.params["tp_multiplier"]
            self.sl_price = self.or_high
            ctx.sell(inst, quantity=self.params["shares"], tag="orb.short")
            self.position_taken = True
            ctx.log("orb_short_entry", price=self.entry_price)
