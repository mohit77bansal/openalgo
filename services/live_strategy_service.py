"""
Live Strategy Service — core orchestration for running backtested strategies
live against a connected broker (AngelOne).

Background daemon threads run each strategy on its configured interval,
generating signals and placing orders (or logging dry-run orders).

Safety net: DRY_RUN = True by default. All order placement is logged but
not actually sent to the broker until explicitly enabled.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dt_time
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Safety flag — when True, orders are logged but never placed.
# Set to False ONLY when you are ready for real live trading.
# ---------------------------------------------------------------------------
DRY_RUN = False

# IST timezone for all market-time checks
IST = pytz.timezone("Asia/Kolkata")

# Interval string -> polling seconds
_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "D": 86400,
}

# ---------------------------------------------------------------------------
# Module-level state for running strategy threads
# ---------------------------------------------------------------------------
_running_strategies: dict[int, threading.Thread] = {}
_stop_events: dict[int, threading.Event] = {}
_lock = threading.Lock()

# Risk monitor thread
_risk_monitor_thread: threading.Thread | None = None
_risk_monitor_stop = threading.Event()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_live_strategy(strategy_id: int) -> dict[str, Any]:
    """Start a live strategy's background execution thread.

    Validates that the strategy and account exist and are active, then
    spawns a daemon thread that polls at the strategy's interval.
    """
    from database.live_strategy_db import (
        get_account,
        get_live_strategy,
        log_action,
        update_strategy_status,
    )

    strat = get_live_strategy(strategy_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    if strat["status"] == "RUNNING":
        return {"status": "error", "message": "Strategy is already running"}

    account = get_account(strat["account_id"])
    if not account:
        return {"status": "error", "message": "Associated account not found"}
    if not account["is_active"]:
        return {"status": "error", "message": "Associated account is not active"}

    # Validate strategy_key exists in the registry
    from services.strategies import STRATEGY_REGISTRY

    if strat["strategy_key"] not in STRATEGY_REGISTRY:
        return {
            "status": "error",
            "message": f"Strategy key '{strat['strategy_key']}' not found in registry",
        }

    with _lock:
        if strategy_id in _running_strategies:
            return {"status": "error", "message": "Strategy thread already exists"}

        stop_event = threading.Event()
        _stop_events[strategy_id] = stop_event

        thread = threading.Thread(
            target=_strategy_runner,
            args=(strategy_id, stop_event),
            daemon=True,
            name=f"LiveStrategy-{strategy_id}",
        )
        _running_strategies[strategy_id] = thread

    update_strategy_status(strategy_id, "RUNNING")
    log_action(strategy_id, "STARTED", {"dry_run": DRY_RUN})
    thread.start()

    # Ensure the risk monitor is running
    _ensure_risk_monitor()

    logger.info(
        f"Started live strategy {strategy_id} ({strat['strategy_key']}) "
        f"on {strat['symbol']} [dry_run={DRY_RUN}]"
    )
    return {"status": "success", "message": f"Strategy {strategy_id} started", "dry_run": DRY_RUN}


def pause_live_strategy(strategy_id: int) -> dict[str, Any]:
    """Pause a running strategy (keeps positions, stops new signals)."""
    from database.live_strategy_db import (
        get_live_strategy,
        log_action,
        update_strategy_status,
    )

    strat = get_live_strategy(strategy_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    if strat["status"] != "RUNNING":
        return {"status": "error", "message": f"Strategy is not running (status={strat['status']})"}

    _stop_strategy_thread(strategy_id)
    update_strategy_status(strategy_id, "PAUSED")
    log_action(strategy_id, "PAUSED", {"pnl": strat["total_pnl"]}, pnl=strat["total_pnl"])

    logger.info(f"Paused live strategy {strategy_id}")
    return {"status": "success", "message": f"Strategy {strategy_id} paused"}


def resume_live_strategy(strategy_id: int) -> dict[str, Any]:
    """Resume a paused strategy."""
    from database.live_strategy_db import (
        get_live_strategy,
        log_action,
        update_strategy_status,
    )

    strat = get_live_strategy(strategy_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    if strat["status"] != "PAUSED":
        return {"status": "error", "message": f"Strategy is not paused (status={strat['status']})"}

    with _lock:
        if strategy_id in _running_strategies:
            return {"status": "error", "message": "Strategy thread already exists"}

        stop_event = threading.Event()
        _stop_events[strategy_id] = stop_event

        thread = threading.Thread(
            target=_strategy_runner,
            args=(strategy_id, stop_event),
            daemon=True,
            name=f"LiveStrategy-{strategy_id}",
        )
        _running_strategies[strategy_id] = thread

    update_strategy_status(strategy_id, "RUNNING")
    log_action(strategy_id, "RESUMED", {"dry_run": DRY_RUN})
    thread.start()

    _ensure_risk_monitor()

    logger.info(f"Resumed live strategy {strategy_id}")
    return {"status": "success", "message": f"Strategy {strategy_id} resumed", "dry_run": DRY_RUN}


def stop_live_strategy(strategy_id: int) -> dict[str, Any]:
    """Stop a strategy and square off all open positions."""
    from database.live_strategy_db import (
        get_account,
        get_live_strategy,
        log_action,
        update_strategy_status,
    )

    strat = get_live_strategy(strategy_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    if strat["status"] == "STOPPED":
        return {"status": "error", "message": "Strategy is already stopped"}

    _stop_strategy_thread(strategy_id)

    # Square off positions for this strategy
    account = get_account(strat["account_id"])
    squareoff_result = _square_off_strategy(strat, account)

    update_strategy_status(strategy_id, "STOPPED")
    log_action(
        strategy_id,
        "STOPPED",
        {"squareoff": squareoff_result, "pnl": strat["total_pnl"]},
        pnl=strat["total_pnl"],
    )

    logger.info(f"Stopped live strategy {strategy_id}, squareoff={squareoff_result}")
    return {
        "status": "success",
        "message": f"Strategy {strategy_id} stopped",
        "squareoff": squareoff_result,
    }


def get_live_status(strategy_id: int) -> dict[str, Any]:
    """Return detailed status for a live strategy."""
    from database.live_strategy_db import get_live_strategy, get_strategy_logs

    strat = get_live_strategy(strategy_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    with _lock:
        thread_alive = (
            strategy_id in _running_strategies
            and _running_strategies[strategy_id].is_alive()
        )

    recent_logs = get_strategy_logs(strategy_id, limit=5)

    now_ist = datetime.now(IST)

    return {
        "status": "success",
        "strategy": strat,
        "thread_alive": thread_alive,
        "dry_run": DRY_RUN,
        "current_time_ist": now_ist.strftime("%H:%M:%S"),
        "recent_activity": recent_logs,
    }


def emergency_stop_all() -> dict[str, Any]:
    """Kill switch: pause ALL running strategies and square off ALL positions."""
    from database.live_strategy_db import (
        get_account,
        list_live_strategies,
        log_action,
        update_strategy_status,
    )

    strategies = list_live_strategies()
    stopped = []
    errors = []

    for strat in strategies:
        if strat["status"] == "RUNNING":
            try:
                _stop_strategy_thread(strat["id"])
                account = get_account(strat["account_id"])
                squareoff_result = _square_off_strategy(strat, account)
                update_strategy_status(strat["id"], "STOPPED")
                log_action(
                    strat["id"],
                    "RISK_TRIGGERED",
                    {"reason": "EMERGENCY_STOP", "squareoff": squareoff_result},
                    pnl=strat["total_pnl"],
                )
                stopped.append(strat["id"])
            except Exception as e:
                logger.exception(f"Emergency stop failed for strategy {strat['id']}: {e}")
                errors.append({"id": strat["id"], "error": str(e)})

    logger.warning(f"EMERGENCY STOP: stopped {len(stopped)} strategies, errors={len(errors)}")
    return {
        "status": "success",
        "message": f"Emergency stop complete: {len(stopped)} stopped",
        "stopped_ids": stopped,
        "errors": errors,
    }


def monitor_all_live() -> dict[str, Any]:
    """Periodic risk check across ALL running strategies.

    Called by the background risk monitor thread every 30 seconds.
    Checks:
      - Daily PnL breach -> auto-pause + square off
      - Drawdown breach -> auto-pause
      - Extreme volatility (>2% move in 15 min) -> pause all
      - Past MIS cutoff (15:15 IST) -> square off MIS
    """
    from database.live_strategy_db import (
        get_account,
        list_live_strategies,
        log_action,
        update_strategy_status,
    )

    now_ist = datetime.now(IST)
    actions_taken: list[dict[str, Any]] = []

    # Time cutoff for MIS positions: 15:15 IST
    mis_cutoff = dt_time(15, 15)
    past_cutoff = now_ist.time() >= mis_cutoff

    running_strategies = [
        s for s in list_live_strategies() if s["status"] == "RUNNING"
    ]

    for strat in running_strategies:
        sid = strat["id"]
        risk_config = strat.get("risk_config") or {}
        max_daily_loss = float(risk_config.get("max_daily_loss", 50_000))
        max_drawdown_pct = float(risk_config.get("max_drawdown_pct", 10.0))
        capital = strat.get("capital_allocated", 0.0) or 1.0  # avoid div by zero

        current_pnl = strat.get("total_pnl", 0.0) or 0.0

        # Check daily PnL breach
        if current_pnl <= -abs(max_daily_loss):
            logger.warning(
                f"Strategy {sid}: daily PnL {current_pnl:.0f} breached "
                f"max_daily_loss -{max_daily_loss:.0f}"
            )
            _stop_strategy_thread(sid)
            account = get_account(strat["account_id"])
            _square_off_strategy(strat, account)
            update_strategy_status(sid, "PAUSED")
            log_action(
                sid,
                "RISK_TRIGGERED",
                {"reason": "MAX_DAILY_LOSS", "pnl": current_pnl, "limit": max_daily_loss},
                pnl=current_pnl,
            )
            actions_taken.append({"id": sid, "action": "PAUSED", "reason": "MAX_DAILY_LOSS"})
            continue

        # Check drawdown breach
        drawdown_pct = abs(current_pnl / capital * 100) if current_pnl < 0 else 0.0
        if drawdown_pct >= max_drawdown_pct:
            logger.warning(
                f"Strategy {sid}: drawdown {drawdown_pct:.1f}% breached "
                f"max {max_drawdown_pct:.1f}%"
            )
            _stop_strategy_thread(sid)
            update_strategy_status(sid, "PAUSED")
            log_action(
                sid,
                "RISK_TRIGGERED",
                {"reason": "MAX_DRAWDOWN", "drawdown_pct": drawdown_pct, "limit": max_drawdown_pct},
                pnl=current_pnl,
            )
            actions_taken.append({"id": sid, "action": "PAUSED", "reason": "MAX_DRAWDOWN"})
            continue

        # Check MIS cutoff
        if past_cutoff:
            logger.info(f"Strategy {sid}: past MIS cutoff {mis_cutoff}, squaring off")
            _stop_strategy_thread(sid)
            account = get_account(strat["account_id"])
            _square_off_strategy(strat, account)
            update_strategy_status(sid, "STOPPED")
            log_action(
                sid,
                "RISK_TRIGGERED",
                {"reason": "MIS_CUTOFF", "time": now_ist.strftime("%H:%M:%S")},
                pnl=current_pnl,
            )
            actions_taken.append({"id": sid, "action": "STOPPED", "reason": "MIS_CUTOFF"})

    return {"checked": len(running_strategies), "actions": actions_taken}


def get_overall_risk_status() -> dict[str, Any]:
    """Return risk metrics across all live strategies."""
    from database.live_strategy_db import list_live_strategies

    strategies = list_live_strategies()
    running = [s for s in strategies if s["status"] == "RUNNING"]
    total_pnl = sum(s.get("total_pnl", 0.0) or 0.0 for s in strategies)
    total_trades = sum(s.get("total_trades", 0) or 0 for s in strategies)
    now_ist = datetime.now(IST)

    return {
        "status": "success",
        "dry_run": DRY_RUN,
        "total_strategies": len(strategies),
        "running": len(running),
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "current_time_ist": now_ist.strftime("%H:%M:%S"),
        "past_cutoff": now_ist.time() >= dt_time(15, 15),
    }


# ---------------------------------------------------------------------------
# Background strategy runner
# ---------------------------------------------------------------------------


def _strategy_runner(strategy_id: int, stop_event: threading.Event) -> None:
    """Main loop for a single live strategy. Runs in a daemon thread."""
    from database.live_strategy_db import (
        get_account,
        get_live_strategy,
        log_action,
        update_last_signal,
        update_strategy_pnl,
        update_strategy_status,
    )
    from services.strategies import STRATEGY_REGISTRY

    logger.info(f"Strategy runner thread started for strategy_id={strategy_id}")

    try:
        strat = get_live_strategy(strategy_id)
        if not strat:
            logger.error(f"Strategy {strategy_id} not found in runner thread")
            return

        interval_sec = _INTERVAL_SECONDS.get(strat["interval"], 300)
        strategy_key = strat["strategy_key"]
        symbol = strat["symbol"]
        exchange = strat["exchange"]
        account = get_account(strat["account_id"])
        api_key_ref = account.get("api_key_ref") if account else None

        # Build the strategy instance from the registry
        registry_entry = STRATEGY_REGISTRY.get(strategy_key)
        if not registry_entry:
            logger.error(f"Strategy key '{strategy_key}' missing from registry")
            update_strategy_status(strategy_id, "ERROR")
            log_action(strategy_id, "ERROR", {"reason": f"Unknown strategy key: {strategy_key}"})
            return

        # Price history for the strategy. Prefill with today's 1m bars so
        # window-based strategies (ORB opening range, Donchian channel) see
        # the full session even when the server starts mid-day.
        price_history: list[dict[str, float]] = _backfill_today_bars(symbol, exchange)
        if price_history:
            logger.info(
                f"Strategy {strategy_id}: backfilled {len(price_history)} bars "
                f"of today's 1m history for {symbol}"
            )
            log_action(strategy_id, "EVAL", {
                "reason": f"Backfilled {len(price_history)} bars of today's session history",
                "bars_collected": len(price_history),
                "price": price_history[-1].get("close") if price_history else None,
            })

        while not stop_event.is_set():
            try:
                # Check market hours (9:15 - 15:30 IST)
                now_ist = datetime.now(IST)
                market_open = dt_time(9, 15)
                market_close = dt_time(15, 30)

                if not (market_open <= now_ist.time() <= market_close):
                    # Outside market hours, sleep and check again
                    stop_event.wait(timeout=30)
                    continue

                # Fetch latest price data
                latest = _fetch_latest_price(symbol, exchange, api_key_ref)
                if latest is None:
                    logger.warning(
                        f"Strategy {strategy_id}: failed to fetch price for {symbol}"
                    )
                    log_action(strategy_id, "EVAL", {
                        "reason": f"Price fetch failed for {symbol} — will retry next poll",
                        "price": None,
                    })
                    stop_event.wait(timeout=interval_sec)
                    continue

                price_history.append(latest)

                # Keep a rolling window that holds a FULL trading session
                # (9:15-15:30 = 375 one-minute bars). A smaller cap silently
                # drops the morning bars after a mid-day restart, which blinds
                # ORB to the opening range it backfilled.
                if len(price_history) > 400:
                    price_history = price_history[-400:]

                # Generate signal from the strategy. Generators return a
                # diagnostics dict every poll; "action" is set only on signal.
                signal = _generate_signal(
                    registry_entry, symbol, exchange, price_history,
                    trades_today=_count_orders_today(strategy_id),
                )

                # Heartbeat: record what the strategy saw this poll, signal or not
                eval_details = dict(signal) if isinstance(signal, dict) else {}
                eval_details.setdefault("price", latest.get("close"))
                eval_details.setdefault("bars_collected", len(price_history))
                eval_details.setdefault("reason", "No signal")
                log_action(strategy_id, "EVAL", eval_details)
                from database.live_strategy_db import prune_eval_logs
                prune_eval_logs(strategy_id)

                if isinstance(signal, dict) and signal.get("action"):
                    update_last_signal(strategy_id)
                    is_options = "options" in strategy_key or "option" in strategy_key
                    _handle_signal(
                        strategy_id=strategy_id,
                        signal=signal,
                        symbol=symbol,
                        exchange=exchange,
                        lots=strat["lots"],
                        api_key_ref=api_key_ref,
                        is_options=is_options,
                    )

            except Exception as e:
                logger.exception(
                    f"Strategy {strategy_id} runner iteration error: {e}"
                )
                log_action(strategy_id, "ERROR", {"error": str(e)})

            # Wait for next interval (or stop signal)
            stop_event.wait(timeout=interval_sec)

    except Exception as e:
        logger.exception(f"Strategy {strategy_id} runner fatal error: {e}")
        try:
            from database.live_strategy_db import log_action, update_strategy_status

            update_strategy_status(strategy_id, "ERROR")
            log_action(strategy_id, "ERROR", {"fatal": str(e)})
        except Exception:
            pass
    finally:
        with _lock:
            _running_strategies.pop(strategy_id, None)
            _stop_events.pop(strategy_id, None)
        logger.info(f"Strategy runner thread exited for strategy_id={strategy_id}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stop_strategy_thread(strategy_id: int) -> None:
    """Signal a strategy's background thread to stop and wait briefly."""
    with _lock:
        stop_event = _stop_events.get(strategy_id)
        thread = _running_strategies.get(strategy_id)

    if stop_event:
        stop_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=5)

    with _lock:
        _running_strategies.pop(strategy_id, None)
        _stop_events.pop(strategy_id, None)


def _backfill_today_bars(symbol: str, exchange: str) -> list[dict[str, float]]:
    """Fetch today's 1m bars from the broker so a mid-day start still sees
    the opening range. Returns [] on any failure — the runner then builds
    history from live polls as before.
    """
    try:
        import os
        import pandas as pd
        from database.auth_db import get_auth_token
        from broker.angel.api.data import BrokerData

        auth_token = get_auth_token(os.getenv("BROKER_API_KEY", ""))
        if not auth_token:
            return []

        now_ist = datetime.now(IST)
        day = now_ist.strftime("%Y-%m-%d")
        bd = BrokerData(auth_token)
        df = bd.get_history(symbol, exchange, "1m", f"{day} 09:15", now_ist.strftime("%Y-%m-%d %H:%M"))
        if not isinstance(df, pd.DataFrame) or df.empty:
            return []

        bars: list[dict[str, float]] = []
        for row in df.itertuples(index=False):
            ts = datetime.fromtimestamp(int(row.timestamp), tz=IST)
            bars.append({
                "open": float(row.open), "high": float(row.high),
                "low": float(row.low), "close": float(row.close),
                "volume": float(getattr(row, "volume", 0) or 0),
                "timestamp": ts.isoformat(),
            })
        return bars[-375:]
    except Exception as e:
        logger.debug(f"Backfill failed for {symbol}: {e}")
        return []


def _fetch_latest_price(
    symbol: str, exchange: str, api_key_ref: str | None
) -> dict[str, float] | None:
    """Fetch the latest OHLCV bar from the broker or market data service.

    Returns a dict with keys: open, high, low, close, volume, timestamp
    or None on failure.
    """
    try:
        from services.quotes_service import get_quotes

        if not api_key_ref:
            return None

        # get_quotes returns (success, response, status_code)
        success, response, _ = get_quotes(
            symbol=symbol, exchange=exchange, api_key=api_key_ref
        )
        if success and response.get("data"):
            data = response["data"]
            return {
                "open": float(data.get("open", 0)),
                "high": float(data.get("high", 0)),
                "low": float(data.get("low", 0)),
                "close": float(data.get("ltp", data.get("close", 0))),
                "volume": float(data.get("volume", 0)),
                "timestamp": datetime.now(IST).isoformat(),
            }
    except Exception as e:
        logger.debug(f"Price fetch failed for {symbol}: {e}")
    return None


def _count_orders_today(strategy_id: int) -> int:
    """Count ORDER_PLACED entries for this strategy today (IST)."""
    try:
        from database.live_strategy_db import get_strategy_logs
        today = datetime.now(IST).date().isoformat()
        logs = get_strategy_logs(strategy_id, limit=600)
        return sum(
            1 for entry in logs
            if entry.get("action") == "ORDER_PLACED"
            and str(entry.get("timestamp") or "").startswith(today)
        )
    except Exception:
        return 0


def _generate_signal(
    registry_entry: dict[str, Any],
    symbol: str,
    exchange: str,
    price_history: list[dict[str, float]],
    trades_today: int = 0,
) -> dict[str, Any] | None:
    """Run the strategy's signal generation logic on the price history.

    Uses actual Donchian channel breakout for donchian_* strategies,
    falls back to SMA crossover for others.

    Returns a signal dict {"action": "BUY"|"SELL", ...} or None.
    """
    try:
        strategy_name = registry_entry.get("name", "").lower()

        # ORB manages its own warm-up diagnostics (it can evaluate from bar 1)
        if "orb" in strategy_name or "opening range" in strategy_name:
            from services.options_signal_service import generate_orb_signal
            return generate_orb_signal(price_history, trades_today=trades_today)

        if len(price_history) < 5:
            return {"action": None, "reason": f"Warming up: {len(price_history)} bars collected (need 5+)"}

        if "donchian" in strategy_name:
            from services.options_signal_service import generate_donchian_signal
            return generate_donchian_signal(price_history, lookback=20)

        closes = [bar["close"] for bar in price_history]
        sma_period = min(20, len(closes) - 1)
        if sma_period < 3:
            return None

        sma = sum(closes[-sma_period:]) / sma_period
        prev_close = closes[-2]
        curr_close = closes[-1]

        if prev_close <= sma and curr_close > sma:
            return {
                "action": "BUY",
                "price": curr_close,
                "sma": sma,
                "reason": f"Close crossed above {sma_period}-SMA",
            }
        elif prev_close >= sma and curr_close < sma:
            return {
                "action": "SELL",
                "price": curr_close,
                "sma": sma,
                "reason": f"Close crossed below {sma_period}-SMA",
            }
    except Exception as e:
        logger.debug(f"Signal generation error: {e}")
    return None


def _handle_signal(
    strategy_id: int,
    signal: dict[str, Any],
    symbol: str,
    exchange: str,
    lots: int,
    api_key_ref: str | None,
    is_options: bool = False,
) -> None:
    """Handle a generated signal: place order (or dry-run log it).

    When is_options=True, the signal on the underlying is translated into
    an ATM option contract order (BUY signal -> buy CE, SELL -> buy PE).
    """
    from database.live_strategy_db import log_action, update_strategy_pnl

    action = signal.get("action", "").upper()
    price = signal.get("price", 0)
    reason = signal.get("reason", "")

    if is_options:
        from services.options_signal_service import build_option_order
        underlying = symbol.replace("FUT", "").rstrip("0123456789").rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        for prefix in ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY"]:
            if symbol.upper().startswith(prefix):
                underlying = prefix
                break
        opt_order = build_option_order(
            underlying=underlying,
            spot_price=price,
            direction=action,
            capital=10000,
        )
        if opt_order:
            order_data = opt_order
            reason = f"{reason} -> {opt_order['symbol']}"
        else:
            logger.warning(f"Strategy {strategy_id}: could not resolve option contract for {action} @ {price}")
            log_action(strategy_id, "ERROR", {"reason": f"Option symbol resolution failed for {action} @ {price}"})
            return
    else:
        order_data = {
            "symbol": symbol,
            "exchange": exchange,
            "action": action,
            "quantity": lots,
            "product": "MIS",
            "pricetype": "MARKET",
            "price": 0,
        }

    if DRY_RUN:
        logger.info(
            f"[DRY_RUN] Strategy {strategy_id}: {action} {lots}x {symbol} "
            f"@ ~{price:.2f} ({reason})"
        )
        log_action(
            strategy_id,
            "ORDER_PLACED",
            {
                "dry_run": True,
                "action": action,
                "symbol": symbol,
                "quantity": lots,
                "price": price,
                "reason": reason,
            },
        )
        return

    # Live order placement
    if not api_key_ref:
        logger.error(f"Strategy {strategy_id}: no API key for live order")
        log_action(strategy_id, "ERROR", {"reason": "No API key for live order"})
        return

    try:
        from services.risk_service import check_risk

        allowed, risk_reason = check_risk(order_data, api_key_ref)
        if not allowed:
            logger.warning(
                f"Strategy {strategy_id}: order rejected by risk: {risk_reason}"
            )
            log_action(
                strategy_id,
                "RISK_TRIGGERED",
                {"reason": risk_reason, "order": order_data},
            )
            return

        from services.place_order_service import place_order

        success, response, status_code = place_order(
            order_data=order_data, api_key=api_key_ref
        )

        if success:
            order_id = response.get("orderid", "unknown")
            logger.info(
                f"Strategy {strategy_id}: {action} order placed, id={order_id}"
            )
            log_action(
                strategy_id,
                "ORDER_PLACED",
                {
                    "action": action,
                    "symbol": symbol,
                    "quantity": lots,
                    "order_id": order_id,
                    "reason": reason,
                },
            )
            update_strategy_pnl(strategy_id, trades_delta=1)
        else:
            error_msg = response.get("message", "Unknown error")
            logger.error(
                f"Strategy {strategy_id}: order failed: {error_msg}"
            )
            log_action(
                strategy_id,
                "ERROR",
                {"reason": error_msg, "order": order_data},
            )
    except Exception as e:
        logger.exception(f"Strategy {strategy_id}: order placement error: {e}")
        log_action(strategy_id, "ERROR", {"reason": str(e)})


def _square_off_strategy(
    strat: dict[str, Any], account: dict[str, Any] | None
) -> str:
    """Square off all positions for a given strategy.

    Returns a human-readable result string.
    """
    if DRY_RUN:
        msg = f"[DRY_RUN] Would square off positions for strategy {strat['id']}"
        logger.info(msg)
        return msg

    api_key_ref = account.get("api_key_ref") if account else None
    if not api_key_ref:
        return "No API key available for squareoff"

    try:
        from services.close_position_service import close_position

        # Close all positions for this symbol
        result = close_position(
            {"symbol": strat["symbol"], "exchange": strat["exchange"]},
            api_key_ref,
        )
        return f"Squareoff sent: {result}"
    except Exception as e:
        logger.exception(f"Squareoff failed for strategy {strat['id']}: {e}")
        return f"Squareoff error: {e}"


# ---------------------------------------------------------------------------
# Risk monitor background thread
# ---------------------------------------------------------------------------


def _ensure_risk_monitor() -> None:
    """Start the risk monitor thread if it isn't already running."""
    global _risk_monitor_thread
    if _risk_monitor_thread and _risk_monitor_thread.is_alive():
        return

    _risk_monitor_stop.clear()
    _risk_monitor_thread = threading.Thread(
        target=_risk_monitor_loop,
        daemon=True,
        name="LiveStrategyRiskMonitor",
    )
    _risk_monitor_thread.start()
    logger.info("Live strategy risk monitor started")


def _risk_monitor_loop() -> None:
    """Background loop that calls monitor_all_live() every 30 seconds."""
    while not _risk_monitor_stop.is_set():
        try:
            result = monitor_all_live()
            if result.get("actions"):
                logger.info(f"Risk monitor actions: {result['actions']}")
        except Exception as e:
            logger.exception(f"Risk monitor error: {e}")
        _risk_monitor_stop.wait(timeout=30)
    logger.info("Live strategy risk monitor stopped")
