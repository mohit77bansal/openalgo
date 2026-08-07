"""
Risk Management Service — pre-trade risk gate for paper and live orders.

Every order (paper or live) MUST pass ``check_risk()`` before placement.
Limits are configurable at runtime via ``update_risk_limits()``.

Design adapted from qbacktest/live/risk.py (aladin engine) with sandbox-
specific additions: symbol/exchange allowlists, notional cap, and PnL
queries against the sandbox fund/position managers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

# IST timezone — all cutoff comparisons use wall-clock IST.
IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=False)
class _RiskLimits:
    """Mutable holder for the current risk configuration.

    A single module-level instance is used; updates go through
    ``update_risk_limits()`` which acquires ``_lock`` first.
    """

    max_daily_loss: float = 50_000.0
    max_open_positions: int = 5
    max_order_value: float = 500_000.0
    max_quantity_per_order: int = 300
    cutoff_time: time = time(15, 15)
    kill_switch: bool = False
    allowed_symbols: list[str] = field(default_factory=lambda: ["NIFTY"])
    allowed_exchanges: list[str] = field(default_factory=lambda: ["NFO"])


_limits = _RiskLimits()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_risk(order_data: dict[str, Any], api_key: str) -> tuple[bool, str]:
    """Return ``(True, "ok")`` if the order passes all risk checks,
    or ``(False, reason)`` if it must be rejected.

    Checks are ordered from cheapest to most expensive (DB queries last).
    """
    with _lock:
        limits = _RiskLimits(
            max_daily_loss=_limits.max_daily_loss,
            max_open_positions=_limits.max_open_positions,
            max_order_value=_limits.max_order_value,
            max_quantity_per_order=_limits.max_quantity_per_order,
            cutoff_time=_limits.cutoff_time,
            kill_switch=_limits.kill_switch,
            allowed_symbols=list(_limits.allowed_symbols),
            allowed_exchanges=list(_limits.allowed_exchanges),
        )

    # 1. Kill switch — instant reject
    if limits.kill_switch:
        logger.warning("Risk REJECTED: kill switch is active")
        return False, "Kill switch is active — all orders blocked"

    # 2. Cutoff time
    now_ist = datetime.now(IST).time()
    if now_ist >= limits.cutoff_time:
        msg = f"Past cutoff time {limits.cutoff_time.strftime('%H:%M')} IST"
        logger.warning(f"Risk REJECTED: {msg}")
        return False, msg

    # 3. Allowed symbols
    symbol = order_data.get("symbol", "")
    if not any(symbol.startswith(prefix) for prefix in limits.allowed_symbols):
        msg = f"Symbol '{symbol}' not in allowed list {limits.allowed_symbols}"
        logger.warning(f"Risk REJECTED: {msg}")
        return False, msg

    # 4. Allowed exchanges
    exchange = order_data.get("exchange", "")
    if exchange not in limits.allowed_exchanges:
        msg = f"Exchange '{exchange}' not in allowed list {limits.allowed_exchanges}"
        logger.warning(f"Risk REJECTED: {msg}")
        return False, msg

    # 5. Max quantity per order
    quantity = int(order_data.get("quantity", 0))
    if quantity > limits.max_quantity_per_order:
        msg = (
            f"Quantity {quantity} exceeds max_quantity_per_order "
            f"({limits.max_quantity_per_order})"
        )
        logger.warning(f"Risk REJECTED: {msg}")
        return False, msg

    # 6. Max order value (quantity * latest price)
    price = float(order_data.get("price", 0))
    if price > 0:
        notional = quantity * price
        if notional > limits.max_order_value:
            msg = (
                f"Order notional {notional:,.0f} exceeds "
                f"max_order_value ({limits.max_order_value:,.0f})"
            )
            logger.warning(f"Risk REJECTED: {msg}")
            return False, msg

    # 7. Max open positions — requires sandbox position query
    try:
        open_count = _count_open_positions(api_key)
        if open_count >= limits.max_open_positions:
            msg = (
                f"Open positions ({open_count}) at or above "
                f"max_open_positions ({limits.max_open_positions})"
            )
            logger.warning(f"Risk REJECTED: {msg}")
            return False, msg
    except Exception as exc:
        logger.error(f"Risk check: failed to query open positions: {exc}")
        # Fail open — do not block trading if the query fails
        pass

    # 8. Max daily loss — requires sandbox fund query
    try:
        daily_pnl = _get_daily_pnl(api_key)
        if daily_pnl <= -abs(limits.max_daily_loss):
            msg = (
                f"Daily PnL ({daily_pnl:,.0f}) breached "
                f"max_daily_loss (-{limits.max_daily_loss:,.0f})"
            )
            logger.warning(f"Risk REJECTED: {msg}")
            return False, msg
    except Exception as exc:
        logger.error(f"Risk check: failed to query daily PnL: {exc}")
        pass

    return True, "ok"


def get_risk_status(api_key: str) -> dict[str, Any]:
    """Return a snapshot of the current risk metrics and configuration."""
    with _lock:
        config = {
            "max_daily_loss": _limits.max_daily_loss,
            "max_open_positions": _limits.max_open_positions,
            "max_order_value": _limits.max_order_value,
            "max_quantity_per_order": _limits.max_quantity_per_order,
            "cutoff_time": _limits.cutoff_time.strftime("%H:%M"),
            "kill_switch": _limits.kill_switch,
            "allowed_symbols": list(_limits.allowed_symbols),
            "allowed_exchanges": list(_limits.allowed_exchanges),
        }

    now_ist = datetime.now(IST)
    past_cutoff = now_ist.time() >= _limits.cutoff_time

    # Best-effort live metrics
    open_positions = 0
    daily_pnl = 0.0
    try:
        open_positions = _count_open_positions(api_key)
    except Exception:
        pass
    try:
        daily_pnl = _get_daily_pnl(api_key)
    except Exception:
        pass

    return {
        "config": config,
        "metrics": {
            "current_time_ist": now_ist.strftime("%H:%M:%S"),
            "past_cutoff": past_cutoff,
            "open_positions": open_positions,
            "daily_pnl": daily_pnl,
        },
    }


def set_kill_switch(active: bool) -> None:
    """Emergency stop — when True, ``check_risk`` rejects every order."""
    with _lock:
        _limits.kill_switch = active
    action = "ENGAGED" if active else "DISENGAGED"
    logger.warning(f"Kill switch {action}")


def update_risk_limits(**kwargs: Any) -> dict[str, Any]:
    """Update any subset of risk limits. Returns the full config after update.

    Accepted keyword arguments match the ``_RiskLimits`` field names:
    ``max_daily_loss``, ``max_open_positions``, ``max_order_value``,
    ``max_quantity_per_order``, ``cutoff_time`` (str "HH:MM" or ``time``),
    ``kill_switch``, ``allowed_symbols``, ``allowed_exchanges``.
    """
    with _lock:
        for key, value in kwargs.items():
            if key == "cutoff_time" and isinstance(value, str):
                h, m = map(int, value.split(":"))
                value = time(h, m)
            if hasattr(_limits, key):
                setattr(_limits, key, value)
            else:
                logger.warning(f"update_risk_limits: unknown key '{key}' ignored")

        # Return a snapshot
        return {
            "max_daily_loss": _limits.max_daily_loss,
            "max_open_positions": _limits.max_open_positions,
            "max_order_value": _limits.max_order_value,
            "max_quantity_per_order": _limits.max_quantity_per_order,
            "cutoff_time": _limits.cutoff_time.strftime("%H:%M"),
            "kill_switch": _limits.kill_switch,
            "allowed_symbols": list(_limits.allowed_symbols),
            "allowed_exchanges": list(_limits.allowed_exchanges),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_open_positions(api_key: str) -> int:
    """Query sandbox positions and return count of non-zero positions."""
    from services.sandbox_service import get_user_id_from_apikey

    user_id = get_user_id_from_apikey(api_key)
    if not user_id:
        return 0

    from sandbox.position_manager import PositionManager

    pm = PositionManager(user_id)
    success, response, _ = pm.get_open_positions()
    if not success:
        return 0

    positions = response.get("data", [])
    return sum(1 for p in positions if p.get("quantity", 0) != 0)


def _get_daily_pnl(api_key: str) -> float:
    """Query sandbox funds and return the daily realized + unrealized PnL."""
    from services.sandbox_service import get_user_id_from_apikey

    user_id = get_user_id_from_apikey(api_key)
    if not user_id:
        return 0.0

    from sandbox.fund_manager import get_user_funds

    funds = get_user_funds(user_id)
    if not funds:
        return 0.0

    # FundManager returns a dict with realized_pnl, unrealized_pnl fields.
    realized = float(funds.get("realized_pnl", 0))
    unrealized = float(funds.get("unrealized_pnl", 0))
    return realized + unrealized
