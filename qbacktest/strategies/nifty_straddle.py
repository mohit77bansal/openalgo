"""NIFTY short straddle — reference strategy.

Strategy:
  At 09:20 IST every trading day, sell ATM CE and ATM PE on the next weekly
  expiry. Hold until either:
    - Combined position loss exceeds `stop_loss_pct` of premium received → exit both legs
    - Combined position gain exceeds `take_profit_pct` of premium received → exit both legs
    - 15:25 IST → square off everything

This is the canonical "premium decay" strategy on NIFTY weeklies. It loses big on
gap days but wins consistently in low-vol regimes. Use as a benchmark.
"""

from __future__ import annotations

from datetime import time
from typing import ClassVar

from qbacktest.core.types import Bar, Side
from qbacktest.engine.context import Context
from qbacktest.engine.strategy import Strategy


class NiftyShortStraddle(Strategy):
    name: ClassVar[str] = "NiftyShortStraddle"

    params: ClassVar[dict] = {
        "underlying": "NIFTY",
        "entry_hour": 9,
        "entry_minute": 20,
        "exit_hour": 15,
        "exit_minute": 25,
        "stop_loss_pct": 0.30,           # 30% of premium → stop loss
        "take_profit_pct": 0.50,         # 50% capture → exit
        "lots": 1,
        "atm_step": 50,
        "max_loss_inr": 25_000.0,        # absolute floor
    }

    def on_start(self, ctx: Context) -> None:
        self.entered_today = False
        self.entry_premium = 0.0   # total premium collected (negative for shorts)
        self.entry_date = None
        self.ce_inst = None
        self.pe_inst = None

    def on_session_open(self, ctx: Context) -> None:
        self.entered_today = False
        self.entry_premium = 0.0
        self.ce_inst = None
        self.pe_inst = None

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        # Only react to underlying index bars
        if bar.instrument.symbol.upper() != self.params["underlying"]:
            return
        now_t = ctx.now.time()

        # Entry trigger
        if not self.entered_today and now_t >= time(self.params["entry_hour"], self.params["entry_minute"]):
            self._enter(ctx, bar.close)
            return

        # Exit trigger — time
        if now_t >= time(self.params["exit_hour"], self.params["exit_minute"]):
            self._exit(ctx)
            return

        # Exit trigger — PnL
        if self.entered_today:
            self._maybe_pnl_exit(ctx)

    def _enter(self, ctx: Context, spot: float) -> None:
        underlying = self.params["underlying"]
        try:
            expiry = ctx.next_weekly_expiry(underlying)
        except ValueError:
            ctx.log("no_weekly_expiry", underlying=underlying)
            return
        atm = ctx.atm_strike(underlying, expiry, step=self.params["atm_step"])
        ce = ctx.option(underlying, expiry, atm, "CE")
        pe = ctx.option(underlying, expiry, atm, "PE")
        ctx.sell(ce, lots=self.params["lots"], tag="straddle.ce")
        ctx.sell(pe, lots=self.params["lots"], tag="straddle.pe")
        self.ce_inst = ce
        self.pe_inst = pe
        self.entered_today = True
        ctx.log("straddle_entered", spot=spot, atm=atm, expiry=str(expiry))

    def _exit(self, ctx: Context) -> None:
        if not self.entered_today:
            return
        ctx.square_off_all()
        ctx.log("straddle_exited_time")
        self.entered_today = False

    def _maybe_pnl_exit(self, ctx: Context) -> None:
        ce_pos = ctx.position(self.ce_inst) if self.ce_inst else None
        pe_pos = ctx.position(self.pe_inst) if self.pe_inst else None
        if ce_pos is None or pe_pos is None:
            return
        unrealized = ce_pos.unrealized_pnl + pe_pos.unrealized_pnl
        # For shorts, unrealized > 0 means premium decayed (we won).
        notional_premium = (
            ce_pos.avg_price * abs(ce_pos.quantity) + pe_pos.avg_price * abs(pe_pos.quantity)
        )
        if notional_premium <= 0:
            return
        if unrealized >= self.params["take_profit_pct"] * notional_premium:
            ctx.square_off_all()
            ctx.log("straddle_exited_tp", pnl=unrealized)
            self.entered_today = False
            return
        if -unrealized >= self.params["stop_loss_pct"] * notional_premium:
            ctx.square_off_all()
            ctx.log("straddle_exited_sl", pnl=unrealized)
            self.entered_today = False
            return
        if unrealized <= -self.params["max_loss_inr"]:
            ctx.square_off_all()
            ctx.log("straddle_exited_max_loss", pnl=unrealized)
            self.entered_today = False
