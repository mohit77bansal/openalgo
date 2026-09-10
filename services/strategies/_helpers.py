"""Shared helper functions used by all strategy modules."""

from __future__ import annotations


def _lot(inst) -> int:
    """Return the lot size for an instrument (minimum 1)."""
    return max(getattr(inst, "lot_size", 1) or 1, 1)


def _atr(highs: list, lows: list, closes: list, period: int) -> float | None:
    """Average True Range over the last `period` bars."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs) / period
