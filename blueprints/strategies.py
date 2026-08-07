# blueprints/strategies.py
# Strategy management surface — lists available strategies, shows config,
# computes signals, and executes (paper or live) via the risk layer.

from flask import Blueprint, jsonify, request

from utils.logging import get_logger

logger = get_logger(__name__)

strategies_bp = Blueprint("strategies_bp", __name__, url_prefix="/strategies")


@strategies_bp.route("/api/list", methods=["GET"])
def list_strategies():
    """List all available automated strategies with their config."""
    from services.strategy_service import get_strategy_info
    return jsonify({
        "status": "success",
        "strategies": [get_strategy_info()],
    })


@strategies_bp.route("/api/signal", methods=["POST"])
def compute_signal():
    """Compute a signal for the ORB strategy given current market state.

    Body: { api_key, or_high, or_low, current_spot, nearest_expiry }
    If or_high/or_low not provided, fetches live NIFTY spot and uses
    the current day's OR (if market is past 09:30).
    """
    params = request.get_json(silent=True) or {}
    or_high = params.get("or_high")
    or_low = params.get("or_low")
    spot = params.get("current_spot")
    expiry = params.get("nearest_expiry")

    if not all([or_high, or_low, spot, expiry]):
        # Try to fetch live spot
        api_key = params.get("api_key")
        if not api_key:
            return jsonify({"status": "error", "message": "provide or_high, or_low, current_spot, nearest_expiry — or api_key for live fetch"}), 400

        try:
            from database.auth_db import get_auth_token_broker
            auth_token, broker = get_auth_token_broker(api_key)
            from broker.angel.api.data import BrokerData
            bd = BrokerData(auth_token)
            quote_resp = bd.get_quotes("NIFTY", "NSE_INDEX")
            if quote_resp and "ltp" in quote_resp:
                spot = spot or float(quote_resp["ltp"])
            if not expiry:
                from database.symbol import get_distinct_expiries
                expiries = get_distinct_expiries(exchange="NFO", underlying="NIFTY")
                expiry = expiries[0] if expiries else None
        except Exception as e:
            logger.exception("failed to fetch live data for signal")
            return jsonify({"status": "error", "message": f"live fetch failed: {e}"}), 500

    if not all([or_high, or_low, spot, expiry]):
        return jsonify({"status": "error", "message": "missing required fields"}), 400

    from services.strategy_service import compute_orb_signal
    sig = compute_orb_signal(
        or_high=float(or_high),
        or_low=float(or_low),
        current_spot=float(spot),
        nearest_expiry=str(expiry),
    )
    if not sig:
        return jsonify({"status": "success", "signal": None, "message": "no valid signal (spot inside OR, or OR range invalid)"})

    return jsonify({
        "status": "success",
        "signal": {
            "direction": sig.direction,
            "trigger_price": sig.trigger_price,
            "buy_strike": sig.buy_strike,
            "sell_strike": sig.sell_strike,
            "buy_symbol": sig.buy_symbol,
            "sell_symbol": sig.sell_symbol,
            "option_type": sig.option_type,
            "spread_width": sig.spread_width,
            "estimated_debit": sig.estimated_debit,
            "max_loss_per_lot": sig.max_loss_per_lot,
            "max_profit_per_lot": sig.max_profit_per_lot,
            "rr_ratio": sig.rr_ratio,
            "or_high": sig.or_high,
            "or_low": sig.or_low,
            "spot_at_signal": sig.spot_at_signal,
            "expiry": sig.expiry,
        },
    })


@strategies_bp.route("/api/execute", methods=["POST"])
def execute_strategy():
    """Execute a strategy signal via paper trading (through risk layer).

    Body: { api_key, or_high, or_low, current_spot, nearest_expiry, lots }
    Computes the signal, validates through risk, places paper orders.
    """
    params = request.get_json(silent=True) or {}
    api_key = params.get("api_key")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key required"}), 400

    or_high = params.get("or_high")
    or_low = params.get("or_low")
    spot = params.get("current_spot")
    expiry = params.get("nearest_expiry")
    lots = int(params.get("lots", 1))

    if not all([or_high, or_low, spot, expiry]):
        return jsonify({"status": "error", "message": "provide or_high, or_low, current_spot, nearest_expiry"}), 400

    from services.strategy_service import compute_orb_signal, execute_spread_paper
    sig = compute_orb_signal(
        or_high=float(or_high),
        or_low=float(or_low),
        current_spot=float(spot),
        nearest_expiry=str(expiry),
    )
    if not sig:
        return jsonify({"status": "success", "executed": False, "message": "no valid signal"})

    result = execute_spread_paper(sig, api_key=api_key, lots=lots)
    return jsonify(result), 200 if result.get("status") == "success" else 500


@strategies_bp.route("/api/multileg/list", methods=["GET"])
def list_multileg():
    """List available multi-leg option strategies."""
    from services.multileg_strategy_service import list_multileg_strategies
    return jsonify({"status": "success", "strategies": list_multileg_strategies()})


@strategies_bp.route("/api/multileg/execute", methods=["POST"])
def execute_multileg():
    """Execute a multi-leg option strategy via paper trading.

    Body: { api_key, strategy (short_straddle|iron_condor|iron_butterfly),
            underlying (NIFTY|BANKNIFTY), expiry (07AUG26), atm_strike (24500),
            lots (1), sell_distance (200), wing_width (100) }
    """
    params = request.get_json(silent=True) or {}
    api_key = params.get("api_key")
    if not api_key:
        return jsonify({"status": "error", "message": "api_key required"}), 400

    from services.multileg_strategy_service import execute_multileg_paper
    result = execute_multileg_paper(
        strategy_key=params.get("strategy", "iron_condor"),
        underlying=params.get("underlying", "NIFTY"),
        expiry=params.get("expiry", "07AUG26"),
        atm_strike=int(params.get("atm_strike", 24500)),
        lots=int(params.get("lots", 1)),
        api_key=api_key,
        sell_distance=int(params.get("sell_distance", 200)),
        wing_width=int(params.get("wing_width", 100)),
    )
    return jsonify(result)
