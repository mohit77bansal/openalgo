"""NSE freeze-quantity table for derivatives.

The NSE rejects single orders exceeding "freeze quantity" — the order has to be
broken into multiple smaller orders. This affects backtest realism: a strategy
trying to put 10000 NIFTY in one shot would actually need ~5 child orders of
1800 each.

Source: NSE F&O segment "Market Wide Position Limit" file. We hardcode current
limits as of 2024 H2; refresh via NSE feed.
"""

from __future__ import annotations


# Current freeze quantities (units, not lots). Active as of Q4 2024.
# When an underlying's freeze quantity changes, add the new value here.
FREEZE_QTY: dict[str, int] = {
    # Indices
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 4200,
    "SENSEX": 1000,
    "BANKEX": 600,
    # Top stock F&O
    "RELIANCE": 12500,
    "TCS": 8750,
    "HDFCBANK": 27500,
    "INFY": 30000,
    "ICICIBANK": 28000,
    "SBIN": 38000,
}


def freeze_qty_for(underlying: str) -> int | None:
    """Return freeze qty for the underlying, or None if unknown."""
    return FREEZE_QTY.get(underlying.upper())


def split_order_qty(underlying: str, total_qty: int, lot_size: int) -> list[int]:
    """Split `total_qty` into child orders respecting freeze qty.

    Each child quantity is a multiple of lot_size and ≤ freeze qty.
    """
    freeze = freeze_qty_for(underlying)
    if freeze is None or total_qty <= freeze:
        return [total_qty]
    # Round freeze down to the nearest multiple of lot_size
    freeze_rounded = (freeze // lot_size) * lot_size
    if freeze_rounded == 0:
        return [total_qty]
    out: list[int] = []
    remaining = total_qty
    while remaining > 0:
        chunk = min(remaining, freeze_rounded)
        out.append(chunk)
        remaining -= chunk
    return out
