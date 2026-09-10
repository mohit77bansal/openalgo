"""JSON API for user order tags (Type + Description annotations)."""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from utils.logging import get_logger

logger = get_logger(__name__)

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "50 per second")

order_tags_bp = Blueprint("order_tags_bp", __name__, url_prefix="/api/v1/ordertags")


def _valid_api_key(api_key: str | None) -> bool:
    if not api_key:
        return False
    from database.auth_db import get_auth_token_broker

    auth_token, _ = get_auth_token_broker(api_key)
    return auth_token is not None


@order_tags_bp.route("/list", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def list_tags():
    """Return all order tags keyed by orderid."""
    from database.order_tags_db import TAG_TYPES, get_all_order_tags

    data = request.get_json(silent=True) or {}
    if not _valid_api_key(data.get("apikey")):
        return jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403

    return jsonify({"status": "success", "data": get_all_order_tags(), "tag_types": TAG_TYPES})


@order_tags_bp.route("/set", methods=["POST"])
@limiter.limit(API_RATE_LIMIT)
def set_tag():
    """Create/update a tag for one order."""
    from database.order_tags_db import upsert_order_tag

    data = request.get_json(silent=True) or {}
    if not _valid_api_key(data.get("apikey")):
        return jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403

    orderid = (data.get("orderid") or "").strip()
    if not orderid:
        return jsonify({"status": "error", "message": "orderid is required"}), 400

    ok = upsert_order_tag(orderid, data.get("tag_type", ""), data.get("description", ""))
    status = "success" if ok else "error"
    return jsonify({"status": status}), (200 if ok else 500)
