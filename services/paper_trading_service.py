"""
Paper Trading Automation Service — programmatic interface to OpenAlgo's
Analyzer/Sandbox engine for automated strategy execution.

Wraps the sandbox subsystem so callers can:
  - Enable/disable paper (analyzer) mode
  - Place paper orders (with pre-trade risk checks)
  - Query positions, funds, and the order book
  - Square off all open positions
"""

from __future__ import annotations

from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Mode management
# ---------------------------------------------------------------------------


def enable_paper_mode(api_key: str) -> dict[str, Any]:
    """Toggle analyzer mode ON and start the sandbox execution + squareoff
    engines.  Returns a status dict suitable for JSON serialisation."""
    from database.settings_db import set_analyze_mode
    from sandbox.execution_thread import (
        is_execution_engine_running,
        start_execution_engine,
    )
    from sandbox.squareoff_thread import (
        is_squareoff_scheduler_running,
        start_squareoff_scheduler,
    )

    set_analyze_mode(True)
    logger.info("Paper trading: analyzer mode ENABLED")

    engine_msg = "already running"
    if not is_execution_engine_running():
        success, engine_msg = start_execution_engine()
        if not success:
            logger.error(f"Paper trading: failed to start execution engine: {engine_msg}")

    scheduler_msg = "already running"
    if not is_squareoff_scheduler_running():
        success, scheduler_msg = start_squareoff_scheduler()
        if not success:
            logger.error(f"Paper trading: failed to start squareoff scheduler: {scheduler_msg}")

    return {
        "status": "success",
        "paper_mode": True,
        "execution_engine": engine_msg,
        "squareoff_scheduler": scheduler_msg,
    }


def disable_paper_mode() -> dict[str, Any]:
    """Toggle analyzer mode OFF and stop the sandbox engines."""
    from database.settings_db import set_analyze_mode
    from sandbox.execution_thread import stop_execution_engine
    from sandbox.squareoff_thread import stop_squareoff_scheduler

    set_analyze_mode(False)

    _, engine_msg = stop_execution_engine()
    _, scheduler_msg = stop_squareoff_scheduler()

    logger.info("Paper trading: analyzer mode DISABLED")

    return {
        "status": "success",
        "paper_mode": False,
        "execution_engine": engine_msg,
        "squareoff_scheduler": scheduler_msg,
    }


# ---------------------------------------------------------------------------
# Order placement (risk-gated)
# ---------------------------------------------------------------------------


def paper_order(
    symbol: str,
    exchange: str,
    action: str,
    quantity: int,
    product: str = "MIS",
    pricetype: str = "MARKET",
    price: float = 0,
    trigger_price: float = 0,
    strategy: str = "paper_auto",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Place a paper order via the sandbox, gated by the risk service.

    Returns a dict with ``status`` ("success" | "error") and order details or
    a rejection reason.
    """
    if not api_key:
        return {"status": "error", "message": "api_key is required"}

    # Ensure analyzer mode is active
    from database.settings_db import get_analyze_mode

    if not get_analyze_mode():
        return {
            "status": "error",
            "message": "Paper mode is not enabled. Call enable_paper_mode() first.",
        }

    order_data = {
        "symbol": symbol,
        "exchange": exchange,
        "action": action.upper(),
        "quantity": int(quantity),
        "product": product,
        "pricetype": pricetype,
        "price": price,
        "trigger_price": trigger_price,
        "strategy": strategy,
    }

    # --- Pre-trade risk check ---
    from services.risk_service import check_risk

    allowed, reason = check_risk(order_data, api_key)
    if not allowed:
        logger.warning(f"Paper order REJECTED by risk: {reason}")
        return {"status": "error", "message": f"Risk rejected: {reason}"}

    # --- Place via the service layer (fires events, logging, etc.) ---
    from services.place_order_service import place_order

    success, response, status_code = place_order(order_data=order_data, api_key=api_key)

    if success:
        return {
            "status": "success",
            "order_id": response.get("orderid"),
            "detail": response,
        }
    else:
        return {
            "status": "error",
            "message": response.get("message", "Order placement failed"),
            "detail": response,
        }


# ---------------------------------------------------------------------------
# Position / fund / orderbook queries
# ---------------------------------------------------------------------------


def get_paper_positions(api_key: str) -> dict[str, Any]:
    """Return all open sandbox positions with current MTM."""
    from services.sandbox_service import sandbox_get_positions

    success, response, status_code = sandbox_get_positions(api_key, {})
    if success:
        return {"status": "success", "data": response.get("data", [])}
    return {"status": "error", "message": response.get("message", "Failed to get positions")}


def get_paper_funds(api_key: str) -> dict[str, Any]:
    """Return sandbox fund status (available capital, used margin, PnL)."""
    from services.sandbox_service import sandbox_get_funds

    success, response, status_code = sandbox_get_funds(api_key, {})
    if success:
        return {"status": "success", "data": response.get("data", {})}
    return {"status": "error", "message": response.get("message", "Failed to get funds")}


def square_off_all(api_key: str) -> dict[str, Any]:
    """Close every open sandbox position."""
    from services.sandbox_service import sandbox_close_position

    success, response, status_code = sandbox_close_position({}, api_key, {})
    if success:
        return {
            "status": "success",
            "message": response.get("message", "All positions closed"),
            "detail": response,
        }
    return {"status": "error", "message": response.get("message", "Square-off failed")}


def get_paper_orderbook(api_key: str) -> dict[str, Any]:
    """Return full sandbox order history."""
    from services.sandbox_service import sandbox_get_orderbook

    success, response, status_code = sandbox_get_orderbook(api_key, {})
    if success:
        return {"status": "success", "data": response.get("data", {})}
    return {"status": "error", "message": response.get("message", "Failed to get orderbook")}
