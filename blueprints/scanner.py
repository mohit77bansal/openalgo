"""
Scanner Blueprint — NIFTY F&O arbitrage scanner API endpoints.

Routes:
  POST /scanner/scan   — one-shot scan, returns opportunities
  POST /scanner/start  — start continuous scanner
  POST /scanner/stop   — stop continuous scanner
  GET  /scanner/status  — scanner status + recent opportunities
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from utils.logging import get_logger

logger = get_logger(__name__)

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "50 per second")

scanner_bp = Blueprint("scanner_bp", __name__, url_prefix="/scanner")


@scanner_bp.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429


# ---------------------------------------------------------------------------
# POST /scanner/scan — one-shot scan
# ---------------------------------------------------------------------------


@scanner_bp.route("/scan", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def scan():
    """Run a single NIFTY option chain scan for arbitrage opportunities.

    Request body (JSON):
        api_key (str, required): OpenAlgo API key.
        include_far_otm (bool, optional): Include far-OTM scanner (default false).
        strike_range (int, optional): Strikes above/below ATM (default 20, 0=all).
    """
    from services.scanner_service import scan_nifty_chain

    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")

    if not api_key:
        return jsonify({"status": "error", "message": "api_key is required"}), 400

    include_far_otm = bool(data.get("include_far_otm", False))
    strike_range = int(data.get("strike_range", 20))

    result = scan_nifty_chain(
        api_key=api_key,
        include_far_otm=include_far_otm,
        strike_range=strike_range,
    )

    status_code = 200 if result.get("status") == "success" else 500
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# POST /scanner/start — start continuous scanner
# ---------------------------------------------------------------------------


@scanner_bp.route("/start", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def start():
    """Start the continuous NIFTY scanner daemon.

    Request body (JSON):
        api_key (str, required): OpenAlgo API key.
        interval (int, optional): Seconds between scans (default 30, min 10).
        auto_execute (bool, optional): Auto-place paper orders (default false).
        threshold (float, optional): Min edge_after_costs_inr to act on (default 100).
        include_far_otm (bool, optional): Include far-OTM scanner (default false).
        strike_range (int, optional): Strikes above/below ATM (default 20, 0=all).
    """
    from services.scanner_service import start_continuous_scanner

    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")

    if not api_key:
        return jsonify({"status": "error", "message": "api_key is required"}), 400

    result = start_continuous_scanner(
        api_key=api_key,
        interval_seconds=int(data.get("interval", 30)),
        auto_execute=bool(data.get("auto_execute", False)),
        threshold_inr=float(data.get("threshold", 100.0)),
        include_far_otm=bool(data.get("include_far_otm", False)),
        strike_range=int(data.get("strike_range", 20)),
    )

    status_code = 200 if result.get("status") == "success" else 409
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# POST /scanner/stop — stop continuous scanner
# ---------------------------------------------------------------------------


@scanner_bp.route("/stop", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def stop():
    """Stop the continuous NIFTY scanner."""
    from services.scanner_service import stop_continuous_scanner

    result = stop_continuous_scanner()
    status_code = 200 if result.get("status") == "success" else 409
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# GET /scanner/status — scanner status + recent opportunities
# ---------------------------------------------------------------------------


@scanner_bp.route("/status", methods=["GET"])
@limiter.limit(API_RATE_LIMIT)
def status():
    """Return the scanner's current status and recent opportunities."""
    from services.scanner_service import get_scanner_status

    return jsonify(get_scanner_status())
