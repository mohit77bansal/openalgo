"""Strategy base class.

User strategies subclass `Strategy` and implement callbacks. The engine wires up a
`Context` providing data, order placement, and position queries.

Example:

    from qbacktest import Strategy

    class NiftyStraddle(Strategy):
        params = {"entry_time": "09:20", "stop_loss_pct": 0.30}

        def on_start(self, ctx):
            self.entered = False

        def on_bar(self, ctx, bar):
            if ctx.now.time().strftime("%H:%M") == self.params["entry_time"] and not self.entered:
                expiry = ctx.next_weekly_expiry("NIFTY")
                atm = ctx.atm_strike("NIFTY", expiry)
                ctx.sell_option("NIFTY", expiry, atm, "CE", lots=1, tag="straddle.ce")
                ctx.sell_option("NIFTY", expiry, atm, "PE", lots=1, tag="straddle.pe")
                self.entered = True

        def on_eod(self, ctx):
            ctx.square_off_all()
            self.entered = False
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from qbacktest.core.types import Bar, Fill, Order, Tick
    from qbacktest.engine.context import Context


class Strategy:
    """Base class for user strategies.

    Subclasses override the callbacks they need. Default implementations are no-ops.
    """

    name: ClassVar[str] = "Strategy"
    params: ClassVar[dict[str, Any]] = {}

    def __init__(self, **overrides: Any) -> None:
        # Allow per-instance parameter overrides without mutating the class dict.
        self.params = {**self.__class__.params, **overrides}

    # --- Lifecycle ------------------------------------------------------------

    def on_start(self, ctx: Context) -> None:
        """Called once before the first market data event."""

    def on_finish(self, ctx: Context) -> None:
        """Called once after the last market data event."""

    # --- Market data ----------------------------------------------------------

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        """Called on each new bar of subscribed instruments."""

    def on_tick(self, ctx: Context, tick: Tick) -> None:
        """Called on each new tick (only when running tick-level)."""

    # --- Order lifecycle ------------------------------------------------------

    def on_order_placed(self, ctx: Context, order: Order) -> None:
        """Called when an order is accepted by the engine."""

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        """Called on each execution against any of the strategy's orders."""

    def on_order_rejected(self, ctx: Context, order: Order) -> None:
        """Called when an order is rejected (margin, freeze qty, circuit, etc)."""

    def on_order_cancelled(self, ctx: Context, order: Order) -> None:
        """Called when an order is cancelled (manually or by validity)."""

    # --- Day boundaries -------------------------------------------------------

    def on_session_open(self, ctx: Context) -> None:
        """Called at session open (09:15 IST) of each trading day."""

    def on_eod(self, ctx: Context) -> None:
        """Called near session close (15:25 IST default) — square off, log, etc."""

    def on_expiry(self, ctx: Context, underlying: str) -> None:
        """Called on expiry day for any underlying with open positions."""

    # --- Helpers --------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} params={self.params}>"
