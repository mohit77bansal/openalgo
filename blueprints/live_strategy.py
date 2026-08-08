"""
Live Strategy Blueprint — JSON API for live strategy execution.

All routes live under ``/live/`` and return JSON. CSRF is exempted because
these endpoints are called programmatically.
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from utils.logging import get_logger

logger = get_logger(__name__)

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "50 per second")

live_strategy_bp = Blueprint("live_strategy_bp", __name__, url_prefix="/live")


@live_strategy_bp.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@live_strategy_bp.route("/accounts", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def list_accounts():
    """List all connected broker accounts."""
    from database.live_strategy_db import list_accounts as db_list_accounts

    accounts = db_list_accounts()
    return jsonify({"status": "success", "data": accounts})


@live_strategy_bp.route("/accounts", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def create_account():
    """Add a new broker account."""
    from database.live_strategy_db import create_account as db_create_account

    data = request.get_json(silent=True) or {}

    required = ["name", "broker", "client_code"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify(
            {"status": "error", "message": f"Missing fields: {', '.join(missing)}"}
        ), 400

    account_id = db_create_account(
        name=data["name"],
        broker=data["broker"],
        client_code=data["client_code"],
        api_key_ref=data.get("api_key_ref"),
    )
    if account_id is None:
        return jsonify({"status": "error", "message": "Failed to create account"}), 500

    return jsonify({"status": "success", "account_id": account_id}), 201


# ---------------------------------------------------------------------------
# Strategies — CRUD
# ---------------------------------------------------------------------------


@live_strategy_bp.route("/strategies", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def list_strategies():
    """List all live strategies with status."""
    from database.live_strategy_db import list_live_strategies

    account_id = request.args.get("account_id", type=int)
    strategies = list_live_strategies(account_id=account_id)
    return jsonify({"status": "success", "data": strategies})


@live_strategy_bp.route("/strategies", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def create_strategy():
    """Create a new live strategy."""
    from database.live_strategy_db import create_live_strategy

    data = request.get_json(silent=True) or {}

    required = ["strategy_key", "account_id", "symbol", "exchange"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify(
            {"status": "error", "message": f"Missing fields: {', '.join(missing)}"}
        ), 400

    # Validate strategy_key exists
    from services.backtest_service import STRATEGY_REGISTRY

    if data["strategy_key"] not in STRATEGY_REGISTRY:
        return jsonify(
            {"status": "error", "message": f"Unknown strategy_key: {data['strategy_key']}"}
        ), 400

    import json

    risk_config_json = None
    if "risk_config" in data and data["risk_config"]:
        risk_config_json = json.dumps(data["risk_config"])

    strategy_id = create_live_strategy(
        strategy_key=data["strategy_key"],
        account_id=int(data["account_id"]),
        symbol=data["symbol"],
        exchange=data["exchange"],
        interval=data.get("interval", "5m"),
        capital_allocated=float(data.get("capital", data.get("capital_allocated", 0))),
        lots=int(data.get("lots", 1)),
        risk_config_json=risk_config_json,
        remarks=data.get("remarks"),
    )
    if strategy_id is None:
        return jsonify({"status": "error", "message": "Failed to create strategy"}), 500

    return jsonify({"status": "success", "strategy_id": strategy_id}), 201


# ---------------------------------------------------------------------------
# Strategies — Lifecycle
# ---------------------------------------------------------------------------


@live_strategy_bp.route("/strategies/<int:strategy_id>/start", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def start_strategy(strategy_id):
    """Start a live strategy."""
    from services.live_strategy_service import start_live_strategy

    result = start_live_strategy(strategy_id)
    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@live_strategy_bp.route("/strategies/<int:strategy_id>/pause", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def pause_strategy(strategy_id):
    """Pause a running strategy."""
    from services.live_strategy_service import pause_live_strategy

    result = pause_live_strategy(strategy_id)
    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@live_strategy_bp.route("/strategies/<int:strategy_id>/resume", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def resume_strategy(strategy_id):
    """Resume a paused strategy."""
    from services.live_strategy_service import resume_live_strategy

    result = resume_live_strategy(strategy_id)
    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@live_strategy_bp.route("/strategies/<int:strategy_id>/stop", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def stop_strategy(strategy_id):
    """Stop a strategy and square off positions."""
    from services.live_strategy_service import stop_live_strategy

    result = stop_live_strategy(strategy_id)
    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# Strategies — Status & Logs
# ---------------------------------------------------------------------------


@live_strategy_bp.route("/strategies/<int:strategy_id>/status", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def strategy_status(strategy_id):
    """Get detailed status for a live strategy."""
    from services.live_strategy_service import get_live_status

    result = get_live_status(strategy_id)
    status_code = 200 if result.get("status") == "success" else 404
    return jsonify(result), status_code


@live_strategy_bp.route("/strategies/<int:strategy_id>/logs", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def strategy_logs(strategy_id):
    """Get audit logs for a live strategy."""
    from database.live_strategy_db import get_strategy_logs

    limit = request.args.get("limit", 100, type=int)
    logs = get_strategy_logs(strategy_id, limit=limit)
    return jsonify({"status": "success", "data": logs})


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


@live_strategy_bp.route("/risk", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def risk_overview():
    """Overall risk status across all live strategies."""
    from services.live_strategy_service import get_overall_risk_status

    return jsonify(get_overall_risk_status())


@live_strategy_bp.route("/emergency-stop", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def emergency_stop():
    """Kill switch: pause ALL strategies and square off ALL positions."""
    from services.live_strategy_service import emergency_stop_all

    result = emergency_stop_all()
    return jsonify(result)
