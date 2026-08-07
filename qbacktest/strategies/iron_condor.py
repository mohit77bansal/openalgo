"""NIFTY iron condor — sells an OTM call spread + OTM put spread.

Construct (4 legs, all same expiry):
    SELL ATM+wing CE
    BUY  ATM+wing+wing_width CE  (defines max loss on call side)
    SELL ATM-wing PE
    BUY  ATM-wing-wing_width PE

Profit = net premium collected if NIFTY closes between the short strikes at expiry.
Max loss = wing_width * lot_size - premium collected.
"""

from __future__ import annotations

from datetime import time
from typing import ClassVar

from qbacktest.core.types import Bar
from qbacktest.engine.context import Context
from qbacktest.engine.strategy import Strategy


class NiftyIronCondor(Strategy):
    name: ClassVar[str] = "NiftyIronCondor"

    params: ClassVar[dict] = {
        "underlying": "NIFTY",
        "entry_hour": 9,
        "entry_minute": 25,
        "exit_hour": 15,
        "exit_minute": 25,
        "wing_distance": 200,          # how far short strikes are from ATM (points)
        "wing_width": 100,             # spread between short and long strikes
        "atm_step": 50,
        "lots": 1,
        "stop_loss_multiplier": 1.5,   # SL when loss = 1.5 × premium received
    }

    def on_start(self, ctx: Context) -> None:
        self.entered = False
        self.legs: list = []

    def on_session_open(self, ctx: Context) -> None:
        self.entered = False
        self.legs = []

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if bar.instrument.symbol.upper() != self.params["underlying"]:
            return
        now_t = ctx.now.time()

        if not self.entered and now_t >= time(self.params["entry_hour"], self.params["entry_minute"]):
            self._enter(ctx, bar.close)
        elif self.entered and now_t >= time(self.params["exit_hour"], self.params["exit_minute"]):
            ctx.square_off_all()
            self.entered = False

    def _enter(self, ctx: Context, spot: float) -> None:
        underlying = self.params["underlying"]
        expiry = ctx.next_weekly_expiry(underlying)
        atm = ctx.atm_strike(underlying, expiry, step=self.params["atm_step"])
        wd = self.params["wing_distance"]
        ww = self.params["wing_width"]
        lots = self.params["lots"]

        # Short call, long call (further OTM)
        short_call = ctx.option(underlying, expiry, atm + wd, "CE")
        long_call = ctx.option(underlying, expiry, atm + wd + ww, "CE")
        # Short put, long put (further OTM)
        short_put = ctx.option(underlying, expiry, atm - wd, "PE")
        long_put = ctx.option(underlying, expiry, atm - wd - ww, "PE")

        ctx.sell(short_call, lots=lots, tag="ic.short_call")
        ctx.buy(long_call, lots=lots, tag="ic.long_call")
        ctx.sell(short_put, lots=lots, tag="ic.short_put")
        ctx.buy(long_put, lots=lots, tag="ic.long_put")

        self.legs = [short_call, long_call, short_put, long_put]
        self.entered = True
        ctx.log("ic_entered", spot=spot, atm=atm, wd=wd, ww=ww)
