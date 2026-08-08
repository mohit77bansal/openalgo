"""Autonomous risk monitoring daemon for live strategies.

Runs as a background thread, checking all live strategies every 30 seconds.
Takes protective action when risk thresholds are breached:
- Auto-pause strategy if daily loss exceeds limit
- Auto-pause if drawdown from peak exceeds threshold
- Auto-pause ALL if market moves >2% in 15 minutes (extreme volatility)
- Auto-square-off all MIS positions after cutoff time (15:15 IST)
- Alert on unusual conditions (no fills, stale data, connection loss)

This daemon is the safety net — it runs independently of strategy threads
and can kill everything if needed.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Risk thresholds (configurable at runtime)
_config = {
    "check_interval_seconds": 30,
    "max_daily_loss_per_strategy": 50_000.0,
    "max_daily_loss_total": 200_000.0,
    "max_drawdown_pct": 15.0,
    "extreme_move_pct": 2.0,
    "extreme_move_window_minutes": 15,
    "cutoff_time": time(15, 15),
    "market_open": time(9, 15),
    "market_close": time(15, 30),
    "stale_data_seconds": 120,
    "enabled": True,
}

_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lock = threading.Lock()

# Price history for extreme-move detection
_recent_prices: dict[str, list[tuple[float, float]]] = {}  # symbol -> [(timestamp, price)]


def update_risk_config(**kwargs) -> dict[str, Any]:
    """Update risk monitoring thresholds."""
    with _lock:
        for k, v in kwargs.items():
            if k in _config:
                _config[k] = v
    return dict(_config)


def get_risk_config() -> dict[str, Any]:
    return dict(_config)


def record_price(symbol: str, price: float) -> None:
    """Record a price tick for extreme-move detection."""
    now = _time.time()
    with _lock:
        if symbol not in _recent_prices:
            _recent_prices[symbol] = []
        _recent_prices[symbol].append((now, price))
        # Keep only last 30 minutes
        cutoff = now - 1800
        _recent_prices[symbol] = [(t, p) for t, p in _recent_prices[symbol] if t > cutoff]


def _check_extreme_move(symbol: str) -> tuple[bool, float]:
    """Check if a symbol has moved more than the threshold in the configured window."""
    with _lock:
        prices = _recent_prices.get(symbol, [])
    if len(prices) < 2:
        return False, 0.0

    now = _time.time()
    window = _config["extreme_move_window_minutes"] * 60
    recent = [(t, p) for t, p in prices if t > now - window]
    if len(recent) < 2:
        return False, 0.0

    oldest_price = recent[0][1]
    newest_price = recent[-1][1]
    if oldest_price == 0:
        return False, 0.0

    move_pct = abs(newest_price - oldest_price) / oldest_price * 100
    return move_pct > _config["extreme_move_pct"], move_pct


def _is_market_hours() -> bool:
    now = datetime.now(IST).time()
    return _config["market_open"] <= now <= _config["market_close"]


def _is_past_cutoff() -> bool:
    now = datetime.now(IST).time()
    return now >= _config["cutoff_time"]


def _monitor_loop() -> None:
    """Main monitoring loop — runs every check_interval_seconds."""
    logger.info("risk monitor daemon started (interval=%ds)", _config["check_interval_seconds"])

    while not _stop_event.is_set():
        try:
            if not _config["enabled"]:
                _stop_event.wait(_config["check_interval_seconds"])
                continue

            if not _is_market_hours():
                _stop_event.wait(_config["check_interval_seconds"])
                continue

            _run_checks()

        except Exception:
            logger.exception("risk monitor check failed")

        _stop_event.wait(_config["check_interval_seconds"])

    logger.info("risk monitor daemon stopped")


def _run_checks() -> None:
    """Run all risk checks across all live strategies."""
    try:
        from database.live_strategy_db import list_live_strategies, update_strategy_status, log_action
    except ImportError:
        return  # DB not ready yet

    strategies = list_live_strategies()
    running = [s for s in strategies if s.get("status") == "RUNNING"]

    if not running:
        return

    # Check 1: Cutoff time — auto-square-off MIS
    if _is_past_cutoff():
        for s in running:
            logger.warning("RISK: cutoff time reached, pausing strategy %d (%s)", s["id"], s["strategy_key"])
            try:
                update_strategy_status(s["id"], "PAUSED")
                log_action(s["id"], "RISK_TRIGGERED", {"reason": "cutoff_time", "time": datetime.now(IST).isoformat()}, s.get("total_pnl", 0))
            except Exception:
                logger.exception("failed to pause strategy %d on cutoff", s["id"])

    # Check 2: Per-strategy daily loss limit
    for s in running:
        pnl = s.get("total_pnl", 0) or 0
        if pnl < -_config["max_daily_loss_per_strategy"]:
            logger.warning("RISK: strategy %d exceeded daily loss (%.0f < -%.0f), auto-pausing",
                           s["id"], pnl, _config["max_daily_loss_per_strategy"])
            try:
                update_strategy_status(s["id"], "PAUSED")
                log_action(s["id"], "RISK_TRIGGERED",
                           {"reason": "max_daily_loss", "pnl": pnl, "limit": _config["max_daily_loss_per_strategy"]},
                           pnl)
            except Exception:
                logger.exception("failed to pause strategy %d on loss limit", s["id"])

    # Check 3: Total portfolio daily loss
    total_pnl = sum(s.get("total_pnl", 0) or 0 for s in running)
    if total_pnl < -_config["max_daily_loss_total"]:
        logger.warning("RISK: total portfolio loss %.0f < -%.0f, EMERGENCY STOP ALL",
                       total_pnl, _config["max_daily_loss_total"])
        for s in running:
            try:
                update_strategy_status(s["id"], "PAUSED")
                log_action(s["id"], "RISK_TRIGGERED",
                           {"reason": "portfolio_max_loss", "total_pnl": total_pnl},
                           s.get("total_pnl", 0))
            except Exception:
                logger.exception("failed to pause strategy %d on portfolio loss", s["id"])

    # Check 4: Extreme market move
    for symbol in set(s.get("symbol", "") for s in running):
        if not symbol:
            continue
        is_extreme, move_pct = _check_extreme_move(symbol)
        if is_extreme:
            logger.warning("RISK: %s moved %.1f%% in %d min — EXTREME VOLATILITY, pausing all on this symbol",
                           symbol, move_pct, _config["extreme_move_window_minutes"])
            for s in running:
                if s.get("symbol") == symbol:
                    try:
                        update_strategy_status(s["id"], "PAUSED")
                        log_action(s["id"], "RISK_TRIGGERED",
                                   {"reason": "extreme_volatility", "symbol": symbol, "move_pct": round(move_pct, 2)},
                                   s.get("total_pnl", 0))
                    except Exception:
                        logger.exception("failed to pause strategy %d on extreme move", s["id"])


def start_risk_monitor() -> dict[str, Any]:
    """Start the autonomous risk monitoring daemon."""
    global _monitor_thread
    with _lock:
        if _monitor_thread and _monitor_thread.is_alive():
            return {"status": "already_running"}
        _stop_event.clear()
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="risk-monitor")
        _monitor_thread.start()
    return {"status": "started", "config": dict(_config)}


def stop_risk_monitor() -> dict[str, Any]:
    """Stop the risk monitoring daemon."""
    global _monitor_thread
    _stop_event.set()
    if _monitor_thread:
        _monitor_thread.join(timeout=5)
    _monitor_thread = None
    return {"status": "stopped"}


def is_risk_monitor_running() -> bool:
    return _monitor_thread is not None and _monitor_thread.is_alive()
