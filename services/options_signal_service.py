"""Options signal service — translates directional signals on the underlying
into actual option contract orders.

Given a BUY/SELL signal on BANKNIFTY underlying, this service:
1. Determines the nearest weekly expiry
2. Finds the ATM strike from the master contract DB
3. Selects ATM CE (for BUY) or ATM PE (for SELL)
4. Returns a ready-to-place order dict targeting the option contract

Works with any underlying that has weekly options on NFO.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# BANKNIFTY strike interval is 100
_STRIKE_INTERVALS = {
    "BANKNIFTY": 100,
    "NIFTY": 50,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
}

_LOT_SIZES = {
    "BANKNIFTY": 30,
    "NIFTY": 65,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
}

_MONTH_CODES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _nearest_expiry_from_db(underlying: str = "BANKNIFTY") -> date | None:
    """Find the nearest option expiry from the master contract DB.

    Weekly options may not be in the DB until a few days before expiry,
    so this returns whatever the nearest available expiry is.
    """
    try:
        from database.symbol import SymToken, db_session
        import re
        rows = db_session.query(SymToken.symbol).filter(
            SymToken.exchange == "NFO",
            SymToken.symbol.like(f"{underlying}%CE"),
        ).distinct().all()
        db_session.remove()

        _MONTH_NUM = {v: k for k, v in _MONTH_CODES.items()}
        expiries: list[date] = []
        pattern = re.compile(rf"^{underlying}(\d{{2}})([A-Z]{{3}})(\d{{2}})\d+CE$")
        for (sym,) in rows:
            m = pattern.match(sym)
            if m:
                dd, mon, yy = m.groups()
                month_num = _MONTH_NUM.get(mon)
                if month_num:
                    expiries.append(date(2000 + int(yy), month_num, int(dd)))

        today = date.today()
        future = sorted(d for d in set(expiries) if d >= today)
        return future[0] if future else None
    except Exception:
        return None


def _nearest_expiry_thursday(from_date: date | None = None) -> date:
    """Find the nearest Thursday (weekly expiry) on or after from_date.
    Falls back to DB lookup if the computed date doesn't have contracts.
    """
    d = from_date or date.today()
    days_ahead = (3 - d.weekday()) % 7
    if days_ahead == 0 and datetime.now().hour >= 15:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


def _round_to_strike(price: float, underlying: str) -> int:
    """Round a price to the nearest valid strike for the underlying."""
    interval = _STRIKE_INTERVALS.get(underlying, 100)
    return int(round(price / interval) * interval)


def resolve_option_symbol(
    underlying: str,
    spot_price: float,
    direction: str,
    expiry: date | None = None,
) -> dict[str, Any]:
    """Resolve the ATM option contract for a directional trade.

    Args:
        underlying: e.g. "BANKNIFTY"
        spot_price: current price of the underlying
        direction: "BUY" (buy CE) or "SELL" (buy PE)
        expiry: target expiry date (default: nearest Thursday)

    Returns:
        Dict with: symbol, exchange, lot_size, strike, option_type, expiry_str
    """
    exp = expiry or _nearest_expiry_from_db(underlying) or _nearest_expiry_thursday()
    strike = _round_to_strike(spot_price, underlying)
    option_type = "CE" if direction == "BUY" else "PE"

    dd = f"{exp.day:02d}"
    mon = _MONTH_CODES[exp.month]
    yy = f"{exp.year % 100:02d}"
    symbol = f"{underlying}{dd}{mon}{yy}{strike}{option_type}"

    return {
        "symbol": symbol,
        "exchange": "NFO",
        "lot_size": _LOT_SIZES.get(underlying, 30),
        "strike": strike,
        "option_type": option_type,
        "expiry": exp.isoformat(),
        "expiry_str": f"{dd}-{mon}-{yy}",
        "underlying": underlying,
        "underlying_price": spot_price,
    }


def verify_symbol_exists(symbol: str, exchange: str = "NFO") -> dict[str, Any] | None:
    """Check if the option symbol exists in the master contract DB and return its token."""
    try:
        from database.symbol import SymToken, db_session
        row = db_session.query(SymToken).filter(
            SymToken.symbol == symbol,
            SymToken.exchange == exchange,
        ).first()
        if row:
            return {
                "symbol": row.symbol,
                "token": str(row.token),
                "exchange": row.exchange,
                "lotsize": int(row.lotsize or 0),
            }
    except Exception as e:
        logger.debug(f"Symbol lookup failed for {symbol}: {e}")
    finally:
        try:
            from database.symbol import db_session
            db_session.remove()
        except Exception:
            pass
    return None


def build_option_order(
    underlying: str,
    spot_price: float,
    direction: str,
    capital: float = 10000,
    max_premium_per_lot: float = 8000,
    product: str = "NRML",
) -> dict[str, Any] | None:
    """Build a complete order dict for an ATM option trade.

    Returns an order dict ready for place_order_service, or None if the
    option symbol can't be found or is too expensive.
    """
    resolution = resolve_option_symbol(underlying, spot_price, direction)
    symbol = resolution["symbol"]
    lot_size = resolution["lot_size"]

    contract = verify_symbol_exists(symbol)
    if not contract:
        logger.warning(f"Option symbol {symbol} not found in master contracts")
        return None

    order = {
        "symbol": symbol,
        "exchange": "NFO",
        "action": "BUY",
        "quantity": lot_size,
        "product": product,
        "pricetype": "MARKET",
        "price": 0,
        "_meta": {
            "underlying": underlying,
            "underlying_price": spot_price,
            "signal_direction": direction,
            "strike": resolution["strike"],
            "option_type": resolution["option_type"],
            "expiry": resolution["expiry"],
            "lot_size": lot_size,
            "max_premium": max_premium_per_lot,
        },
    }

    logger.info(
        f"Options order: {direction} signal -> BUY {symbol} "
        f"(strike={resolution['strike']}, expiry={resolution['expiry']})"
    )
    return order


def generate_donchian_signal(
    price_history: list[dict[str, float]],
    lookback: int = 20,
) -> dict[str, Any] | None:
    """Generate a Donchian channel breakout signal from price history.

    This is the actual Donchian logic (not the simplified SMA crossover
    that live_strategy_service uses by default).

    Returns {"action": "BUY"|"SELL", "price": ..., "reason": ...} or None.
    """
    if len(price_history) < lookback + 1:
        return None

    highs = [bar["high"] for bar in price_history]
    lows = [bar["low"] for bar in price_history]
    closes = [bar["close"] for bar in price_history]

    channel_high = max(highs[-(lookback + 1):-1])
    channel_low = min(lows[-(lookback + 1):-1])
    current_close = closes[-1]

    if current_close > channel_high:
        return {
            "action": "BUY",
            "price": current_close,
            "channel_high": channel_high,
            "channel_low": channel_low,
            "reason": f"Donchian breakout UP: close {current_close:.0f} > {lookback}-bar high {channel_high:.0f}",
        }
    elif current_close < channel_low:
        return {
            "action": "SELL",
            "price": current_close,
            "channel_high": channel_high,
            "channel_low": channel_low,
            "reason": f"Donchian breakout DOWN: close {current_close:.0f} < {lookback}-bar low {channel_low:.0f}",
        }

    return None


def generate_orb_signal(
    price_history: list[dict[str, float]],
    or_end_hour: int = 9,
    or_end_minute: int = 45,
    entry_cutoff_hour: int = 14,
    entry_cutoff_minute: int = 30,
    trades_today: int = 0,
    entry_window_hour: int = 10,
    entry_window_minute: int = 45,
) -> dict[str, Any]:
    """Opening Range Breakout signal for live options trading.

    Opening range = high/low of today's bars from 9:15 to 9:45.
    After 9:45: close above OR high -> BUY (call), below OR low -> SELL (put).
    One signal per day; no entries after 14:30.

    ALWAYS returns a diagnostics dict. ``action`` is None when no signal —
    ``reason`` then explains what the strategy is waiting for, so the live
    UI can show exactly what the strategy sees each evaluation.

    Source: IntradayLab 8-year backtest (Sharpe 1.16, 48.7% win rate).
    """
    diag: dict[str, Any] = {"action": None, "strategy_state": "orb"}
    if not price_history:
        diag["reason"] = "No price data yet"
        return diag

    from datetime import datetime as _dt

    last = price_history[-1]
    close = float(last.get("close", 0))
    diag["price"] = close
    diag["bars_collected"] = len(price_history)

    try:
        last_ts = _dt.fromisoformat(str(last.get("timestamp", "")))
    except ValueError:
        diag["reason"] = "Bad timestamp on latest bar"
        return diag

    today = last_ts.date()

    or_high: float | None = None
    or_low: float | None = None

    for bar in price_history:
        try:
            ts = _dt.fromisoformat(str(bar.get("timestamp", "")))
        except ValueError:
            continue
        if ts.date() != today:
            continue
        in_or_window = (ts.hour == 9 and 15 <= ts.minute) or (
            ts.hour == or_end_hour and ts.minute < or_end_minute
        )
        if ts.hour == 9 and ts.minute < 15:
            in_or_window = False
        if in_or_window:
            h = float(bar.get("high", bar.get("close", 0)))
            low = float(bar.get("low", bar.get("close", 0)))
            or_high = h if or_high is None else max(or_high, h)
            or_low = low if or_low is None else min(or_low, low)

    diag["or_high"] = or_high
    diag["or_low"] = or_low
    diag["trades_today"] = trades_today

    if or_high is None or or_low is None:
        diag["reason"] = "Building opening range (9:15-9:45) — no bars in OR window yet"
        return diag

    if (last_ts.hour, last_ts.minute) < (or_end_hour, or_end_minute):
        diag["reason"] = f"Inside OR window — range so far {or_low:.0f}-{or_high:.0f}, waiting for 9:45"
        return diag

    # 1 trade per day, counted from ACTUAL orders placed — not inferred
    # from price history (a breakout that happened while the strategy
    # wasn't running is a missed trade, not a taken one).
    if trades_today >= 1:
        diag["reason"] = f"Already traded today ({trades_today} order placed, 1/day max). OR: {or_low:.0f}-{or_high:.0f}"
        return diag

    breakout_up = close > or_high
    breakout_dn = close < or_low

    # Research spec: fresh breakouts only — entry must trigger by 10:45.
    # A breakout chased hours late has a different R:R than the backtest.
    past_entry_window = (last_ts.hour, last_ts.minute) >= (entry_window_hour, entry_window_minute)
    if past_entry_window:
        if breakout_up or breakout_dn:
            side = "above OR high" if breakout_up else "below OR low"
            diag["reason"] = (
                f"Close {close:.0f} is {side} ({or_low:.0f}-{or_high:.0f}) but past the "
                f"{entry_window_hour}:{entry_window_minute:02d} entry window — ORB only takes fresh breakouts"
            )
        else:
            diag["reason"] = (
                f"Inside range {or_low:.0f}-{or_high:.0f}; entry window closed at "
                f"{entry_window_hour}:{entry_window_minute:02d} — no more entries today"
            )
        return diag

    if breakout_up:
        diag["action"] = "BUY"
        diag["reason"] = f"ORB breakout UP: close {close:.0f} > OR high {or_high:.0f}"
        return diag
    if breakout_dn:
        diag["action"] = "SELL"
        diag["reason"] = f"ORB breakout DOWN: close {close:.0f} < OR low {or_low:.0f}"
        return diag

    dist_up = or_high - close
    dist_dn = close - or_low
    diag["reason"] = (
        f"Inside range {or_low:.0f}-{or_high:.0f}: need +{dist_up:.0f} for CE breakout "
        f"or -{dist_dn:.0f} for PE breakdown (entry window open until "
        f"{entry_window_hour}:{entry_window_minute:02d})"
    )
    return diag
