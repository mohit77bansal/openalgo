"""
Scanner Service — NIFTY F&O arbitrage scanner pipeline.

Wraps the qbacktest arb scanners (box spread, put-call parity, conversion/
reversal, adjacent-strike inversion, far-OTM premium) with live data from
the OpenAlgo chain service.

Modes:
  - One-shot: ``scan_nifty_chain(api_key)`` — single scan, returns JSON.
  - Continuous: ``start_continuous_scanner(...)`` — background daemon thread
    scanning every N seconds during market hours (09:15-15:30 IST), with
    optional auto-execution via the paper trading service.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Optional

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Market hours (IST)
_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)

# Maximum recent opportunities to keep in memory
_MAX_RECENT = 200


# ---------------------------------------------------------------------------
# Internal state (module-level singleton — one scanner per process)
# ---------------------------------------------------------------------------


@dataclass
class _ScannerState:
    """Mutable holder for the continuous scanner's runtime state."""

    running: bool = False
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_scan_ts: Optional[str] = None
    total_scans: int = 0
    total_opportunities: int = 0
    recent_opportunities: deque = field(default_factory=lambda: deque(maxlen=_MAX_RECENT))
    config: dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None


_state = _ScannerState()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_market_hours() -> bool:
    """Return True if current IST time is within market hours."""
    now = datetime.now(IST).time()
    return _MARKET_OPEN <= now <= _MARKET_CLOSE


def _resolve_auth_token(api_key: str) -> str:
    """Convert an OpenAlgo API key to a broker auth token.

    Raises ValueError if the key is invalid or the broker session is expired.
    """
    from database.auth_db import get_auth_token_broker

    result = get_auth_token_broker(api_key, include_feed_token=False)
    auth_token, _broker = result
    if auth_token is None:
        raise ValueError(
            "Invalid or expired API key — cannot obtain broker auth token. "
            "Is AngelOne connected?"
        )
    return auth_token


def _opportunity_to_dict(opp: Any, scan_ts: str) -> dict[str, Any]:
    """Serialize an Opportunity + timestamp for JSON / in-memory storage."""
    data = opp.to_json()
    data["detected_at"] = scan_ts
    return data


# ---------------------------------------------------------------------------
# One-shot scan
# ---------------------------------------------------------------------------


def scan_nifty_chain(
    api_key: str,
    include_far_otm: bool = False,
    strike_range: int = 20,
) -> dict[str, Any]:
    """Run a single scan of the NIFTY option chain for arbitrage opportunities.

    Args:
        api_key: OpenAlgo API key (used to resolve broker auth token).
        include_far_otm: Whether to include the FarOTMPremiumScanner (quasi-arb).
        strike_range: Strikes above/below ATM to include (0 = all).

    Returns:
        Dict with ``status``, ``opportunities`` list, ``chain_summary``, and ``timing``.
    """
    try:
        t0 = time.monotonic()

        auth_token = _resolve_auth_token(api_key)

        from services.chain_service import build_nifty_chain

        snapshot = build_nifty_chain(
            auth_token=auth_token,
            underlying="NIFTY",
            exchange="NFO",
            strike_range=strike_range,
        )

        from qbacktest.strategies.arbitrage.scan_all import scan_all

        opportunities = scan_all(snapshot, include_far_otm=include_far_otm)

        elapsed = time.monotonic() - t0
        scan_ts = datetime.now(IST).isoformat()

        opp_dicts = [_opportunity_to_dict(o, scan_ts) for o in opportunities]

        return {
            "status": "success",
            "scan_time": scan_ts,
            "elapsed_seconds": round(elapsed, 2),
            "chain_summary": {
                "underlying": snapshot.underlying,
                "expiry": snapshot.expiry.isoformat(),
                "spot": snapshot.spot,
                "futures_price": snapshot.futures_price,
                "strikes": len(snapshot.strikes),
                "quotes": len(snapshot.quotes),
                "lot_size": snapshot.lot_size,
            },
            "opportunity_count": len(opp_dicts),
            "opportunities": opp_dicts,
        }

    except ValueError as exc:
        logger.warning(f"Scan failed (validation): {exc}")
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        logger.exception(f"Scan failed: {exc}")
        return {"status": "error", "message": f"Scan failed: {exc}"}


# ---------------------------------------------------------------------------
# Continuous scanner
# ---------------------------------------------------------------------------


def start_continuous_scanner(
    api_key: str,
    interval_seconds: int = 30,
    auto_execute: bool = False,
    threshold_inr: float = 100.0,
    include_far_otm: bool = False,
    strike_range: int = 20,
) -> dict[str, Any]:
    """Start a background daemon thread that scans continuously during market hours.

    Args:
        api_key: OpenAlgo API key.
        interval_seconds: Seconds between scans (minimum 10).
        auto_execute: If True, auto-execute opportunities above threshold via paper orders.
        threshold_inr: Minimum ``edge_after_costs_inr`` to log/execute (default 100).
        include_far_otm: Include the FarOTMPremiumScanner.
        strike_range: Strikes above/below ATM (0 = all).

    Returns:
        Status dict.
    """
    with _lock:
        if _state.running:
            return {
                "status": "error",
                "message": "Scanner is already running. Stop it first.",
            }

        interval_seconds = max(10, interval_seconds)

        _state.stop_event.clear()
        _state.config = {
            "api_key": api_key,
            "interval_seconds": interval_seconds,
            "auto_execute": auto_execute,
            "threshold_inr": threshold_inr,
            "include_far_otm": include_far_otm,
            "strike_range": strike_range,
        }

        thread = threading.Thread(
            target=_scanner_loop,
            args=(
                api_key,
                interval_seconds,
                auto_execute,
                threshold_inr,
                include_far_otm,
                strike_range,
            ),
            daemon=True,
            name="NiftyScannerLoop",
        )
        thread.start()

        _state.thread = thread
        _state.running = True
        _state.last_error = None

        logger.info(
            f"Continuous scanner started: interval={interval_seconds}s, "
            f"auto_execute={auto_execute}, threshold={threshold_inr} INR"
        )

        return {
            "status": "success",
            "message": "Continuous scanner started",
            "config": {
                "interval_seconds": interval_seconds,
                "auto_execute": auto_execute,
                "threshold_inr": threshold_inr,
                "include_far_otm": include_far_otm,
                "strike_range": strike_range,
            },
        }


def stop_continuous_scanner() -> dict[str, Any]:
    """Stop the background scanner thread."""
    with _lock:
        if not _state.running:
            return {"status": "error", "message": "Scanner is not running."}

        _state.stop_event.set()
        _state.running = False

        logger.info("Continuous scanner stop requested")

        return {
            "status": "success",
            "message": "Scanner stop signal sent",
            "total_scans": _state.total_scans,
            "total_opportunities": _state.total_opportunities,
        }


def get_scanner_status() -> dict[str, Any]:
    """Return the current scanner status and recent opportunities."""
    with _lock:
        recent = list(_state.recent_opportunities)

    return {
        "running": _state.running,
        "last_scan_ts": _state.last_scan_ts,
        "total_scans": _state.total_scans,
        "total_opportunities": _state.total_opportunities,
        "last_error": _state.last_error,
        "config": dict(_state.config) if _state.config else None,
        "is_market_hours": _is_market_hours(),
        "recent_opportunities": recent[-20:],  # last 20 for the status endpoint
    }


# ---------------------------------------------------------------------------
# Scanner loop (runs on daemon thread)
# ---------------------------------------------------------------------------


def _scanner_loop(
    api_key: str,
    interval_seconds: int,
    auto_execute: bool,
    threshold_inr: float,
    include_far_otm: bool,
    strike_range: int,
) -> None:
    """Main loop for the continuous scanner. Runs until stop_event is set."""
    logger.info("Scanner loop thread started")

    while not _state.stop_event.is_set():
        # Sleep outside market hours (check every 30s)
        if not _is_market_hours():
            now_ist = datetime.now(IST).strftime("%H:%M")
            logger.debug(
                f"Outside market hours ({now_ist} IST), sleeping 30s..."
            )
            _state.stop_event.wait(timeout=30)
            continue

        try:
            _run_single_scan(
                api_key=api_key,
                auto_execute=auto_execute,
                threshold_inr=threshold_inr,
                include_far_otm=include_far_otm,
                strike_range=strike_range,
            )
        except Exception as exc:
            error_msg = f"Scanner loop error: {exc}"
            logger.exception(error_msg)
            with _lock:
                _state.last_error = error_msg

        # Wait for the interval (or until stop is requested)
        _state.stop_event.wait(timeout=interval_seconds)

    # Clean up
    with _lock:
        _state.running = False
        _state.thread = None

    logger.info("Scanner loop thread exited")


def _run_single_scan(
    api_key: str,
    auto_execute: bool,
    threshold_inr: float,
    include_far_otm: bool,
    strike_range: int,
) -> None:
    """Execute one scan cycle: build chain -> run scanners -> process results."""
    t0 = time.monotonic()

    auth_token = _resolve_auth_token(api_key)

    from services.chain_service import build_nifty_chain

    snapshot = build_nifty_chain(
        auth_token=auth_token,
        underlying="NIFTY",
        exchange="NFO",
        strike_range=strike_range,
    )

    from qbacktest.strategies.arbitrage.scan_all import scan_all

    opportunities = scan_all(snapshot, include_far_otm=include_far_otm)

    elapsed = time.monotonic() - t0
    scan_ts = datetime.now(IST).isoformat()

    with _lock:
        _state.total_scans += 1
        _state.last_scan_ts = scan_ts
        _state.last_error = None

    # Filter to actionable opportunities
    actionable = [o for o in opportunities if o.edge_after_costs_inr >= threshold_inr]

    if actionable:
        logger.info(
            f"Scan #{_state.total_scans}: {len(actionable)} actionable "
            f"opportunities (of {len(opportunities)} total) in {elapsed:.1f}s"
        )
    else:
        logger.debug(
            f"Scan #{_state.total_scans}: {len(opportunities)} opportunities "
            f"(none above threshold {threshold_inr} INR) in {elapsed:.1f}s"
        )

    for opp in actionable:
        opp_dict = _opportunity_to_dict(opp, scan_ts)

        with _lock:
            _state.total_opportunities += 1
            _state.recent_opportunities.append(opp_dict)

        logger.info(
            f"  {opp.kind.value}: edge={opp.edge_after_costs_inr:.2f} INR, "
            f"legs={len(opp.legs)}, {opp.notes}"
        )

        # Emit via socketio (best-effort, no crash if socketio unavailable)
        try:
            from extensions import socketio

            socketio.emit(
                "scanner_opportunity",
                opp_dict,
                namespace="/",
            )
        except Exception:
            pass

        # Auto-execute via paper trading if enabled
        if auto_execute:
            _auto_execute_opportunity(opp, api_key, snapshot)


def _auto_execute_opportunity(
    opp: Any,
    api_key: str,
    snapshot: Any,
) -> None:
    """Place paper orders for each leg of an opportunity.

    Each leg becomes a separate paper order. Failures are logged but do not
    halt the scanner.
    """
    from services.paper_trading_service import paper_order

    for leg in opp.legs:
        try:
            result = paper_order(
                symbol=leg.instrument_id,
                exchange="NFO",
                action=leg.side.value,
                quantity=leg.quantity,
                product="MIS",
                pricetype="LIMIT",
                price=leg.price,
                strategy=f"scanner_{opp.kind.value.lower()}",
                api_key=api_key,
            )

            if result.get("status") == "success":
                logger.info(
                    f"  Paper order placed: {leg.side.value} {leg.quantity} "
                    f"{leg.instrument_id} @ {leg.price} -> {result.get('order_id')}"
                )
            else:
                logger.warning(
                    f"  Paper order failed: {leg.instrument_id} -> "
                    f"{result.get('message')}"
                )
        except Exception as exc:
            logger.error(
                f"  Paper order exception for {leg.instrument_id}: {exc}"
            )
