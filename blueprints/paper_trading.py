"""
Paper Trading Blueprint — JSON API for the paper trading automation service
and the risk management layer.

All routes live under ``/paper/`` and return JSON.  CSRF is exempted because
these endpoints are called programmatically (API key in the request body
provides authentication).
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from utils.logging import get_logger

logger = get_logger(__name__)

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "50 per second")

paper_trading_bp = Blueprint("paper_trading_bp", __name__, url_prefix="/paper")


@paper_trading_bp.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429


# ---------------------------------------------------------------------------
# Mode management
# ---------------------------------------------------------------------------


@paper_trading_bp.route("/enable", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def enable():
    """Enable paper (analyzer) mode and start sandbox engines."""
    from services.paper_trading_service import enable_paper_mode

    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key is required"}), 400

    result = enable_paper_mode(api_key)
    return jsonify(result)


@paper_trading_bp.route("/disable", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def disable():
    """Disable paper mode and stop sandbox engines."""
    from services.paper_trading_service import disable_paper_mode

    result = disable_paper_mode()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


@paper_trading_bp.route("/order", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def order():
    """Place a paper order (risk-gated)."""
    from services.paper_trading_service import paper_order

    data = request.get_json(silent=True) or {}

    required = ["symbol", "exchange", "action", "quantity"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"status": "error", "message": f"Missing fields: {', '.join(missing)}"}), 400

    api_key = data.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key is required"}), 400

    result = paper_order(
        symbol=data["symbol"],
        exchange=data["exchange"],
        action=data["action"],
        quantity=int(data["quantity"]),
        product=data.get("product", "MIS"),
        pricetype=data.get("pricetype", "MARKET"),
        price=float(data.get("price", 0)),
        trigger_price=float(data.get("trigger_price", 0)),
        strategy=data.get("strategy", "paper_auto"),
        api_key=api_key,
    )

    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@paper_trading_bp.route("/positions", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def positions():
    """Get all open sandbox positions with current MTM."""
    from services.paper_trading_service import get_paper_positions

    api_key = request.args.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key query param required"}), 400
    return jsonify(get_paper_positions(api_key))


@paper_trading_bp.route("/funds", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def funds():
    """Get sandbox fund status (available capital, margin, PnL)."""
    from services.paper_trading_service import get_paper_funds

    api_key = request.args.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key query param required"}), 400
    return jsonify(get_paper_funds(api_key))


@paper_trading_bp.route("/squareoff", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def squareoff():
    """Square off all open sandbox positions."""
    from services.paper_trading_service import square_off_all

    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key is required"}), 400
    return jsonify(square_off_all(api_key))


@paper_trading_bp.route("/orderbook", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def orderbook():
    """Get full sandbox order history."""
    from services.paper_trading_service import get_paper_orderbook

    api_key = request.args.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key query param required"}), 400
    return jsonify(get_paper_orderbook(api_key))


# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------


@paper_trading_bp.route("/risk", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def risk_status():
    """Get current risk metrics and configuration."""
    from services.risk_service import get_risk_status

    api_key = request.args.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key query param required"}), 400
    return jsonify({"status": "success", **get_risk_status(api_key)})


@paper_trading_bp.route("/killswitch", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def killswitch():
    """Toggle the kill switch (emergency stop)."""
    from services.risk_service import set_kill_switch

    data = request.get_json(silent=True) or {}
    if "active" not in data:
        return jsonify({"status": "error", "message": "'active' (bool) is required"}), 400

    active = bool(data["active"])
    set_kill_switch(active)
    return jsonify({"status": "success", "kill_switch": active})
