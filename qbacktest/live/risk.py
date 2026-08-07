"""Risk limits for live trading.

Hard guardrails that cannot be bypassed by strategy logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta


@dataclass
class RiskLimits:
    """Per-day risk caps. Engine checks before every order."""

    max_daily_loss_inr: float = 50_000.0
    max_position_value_inr: float = 5_000_000.0
    max_orders_per_day: int = 200
    max_open_positions: int = 20
    allow_naked_options_sell: bool = False  # require hedge if true; sells are blocked otherwise
    cutoff_time_ist: time = time(15, 25)
    kill_switch_engaged: bool = False

    # Trading-day state (reset at session open)
    _orders_today: int = field(default=0, init=False)
    _realized_today: float = field(default=0.0, init=False)
    _session_date: datetime | None = field(default=None, init=False)

    def reset_for_session(self, session_dt: datetime) -> None:
        self._orders_today = 0
        self._realized_today = 0.0
        self._session_date = session_dt

    def record_order(self) -> None:
        self._orders_today += 1

    def record_realized(self, delta: float) -> None:
        self._realized_today += delta

    def check_can_trade(self, *, current_time: datetime, n_open_positions: int) -> tuple[bool, str]:
        if self.kill_switch_engaged:
            return False, "Kill switch engaged"
        if current_time.time() >= self.cutoff_time_ist:
            return False, f"Past cutoff time {self.cutoff_time_ist}"
        if self._orders_today >= self.max_orders_per_day:
            return False, f"Order count cap {self.max_orders_per_day} reached"
        if -self._realized_today >= self.max_daily_loss_inr:
            return False, f"Max daily loss ₹{self.max_daily_loss_inr:,.0f} hit"
        if n_open_positions >= self.max_open_positions:
            return False, f"Max open positions {self.max_open_positions} reached"
        return True, "ok"

    def engage_kill_switch(self, reason: str = "manual") -> None:
        self.kill_switch_engaged = True
