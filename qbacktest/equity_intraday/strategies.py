"""Intraday equity screener signals — all look-ahead-safe.

At 09:15 (entry) the only things known about *today* are the OPEN and the gap
vs yesterday's close. Today's high/low/close/volume are NOT known yet, so no
signal here uses them — they only ever appear in the exit (backtest.py). Every
filter keys off: the open, the gap, and prior-day rolling features (liquidity,
ATR, yesterday's high/low). Using today's full-day volume as an entry filter
would be look-ahead bias; we deliberately don't.

The screen is two stages, matching the ask ("filter 2-3K stocks, then act"):
  1. Universe filter — liquidity + price sanity (drops ~90% of the 2-3K).
  2. Signal — gap momentum / gap fade / prior-range breakout on the survivors,
     ranked by |gap| so we take the strongest setups first.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from qbacktest.equity_intraday.backtest import Signal

# Universe filter defaults.
MIN_AVG_TURNOVER_LACS = 2000.0   # >= ~Rs 20 cr/day 20d avg — intraday-liquid
PRICE_MIN, PRICE_MAX = 50.0, 10000.0
DEFAULT_STOP_PCT = 0.02


def _eligible(panel: pd.DataFrame) -> pd.DataFrame:
    """Rows that pass the liquidity + price-sanity + valid-feature filter."""
    m = (
        (panel["avg_turnover20"] >= MIN_AVG_TURNOVER_LACS)
        & (panel["prev_close"] >= PRICE_MIN)
        & (panel["prev_close"] <= PRICE_MAX)
        & panel["atr20_pct"].notna()
        & (panel["open"] > 0)
    )
    return panel[m]


def _emit(df: pd.DataFrame, direction: int, stop_pct: float) -> dict[date, list[Signal]]:
    """Turn a filtered, ranked frame into day -> [Signal]."""
    out: dict[date, list[Signal]] = {}
    for d, grp in df.groupby("date"):
        grp = grp.reindex(grp["gap_pct"].abs().sort_values(ascending=False).index)
        out[d] = [
            Signal(date=d, symbol=r.symbol, direction=direction, entry=float(r.open),
                   stop_pct=stop_pct)
            for r in grp.itertuples()
        ]
    return out


def gap_up_momentum(panel: pd.DataFrame, *, gap: float = 0.02,
                    stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Gap up + prior-day strength → long, betting the gap continues."""
    e = _eligible(panel)
    e = e[(e["gap_pct"] >= gap) & (e["prev_ret_cc"] > 0)]
    return _emit(e, +1, stop_pct)


def gap_down_momentum(panel: pd.DataFrame, *, gap: float = 0.02,
                      stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Gap down + prior-day weakness → short, betting the drop continues."""
    e = _eligible(panel)
    e = e[(e["gap_pct"] <= -gap) & (e["prev_ret_cc"] < 0)]
    return _emit(e, -1, stop_pct)


def gap_up_fade(panel: pd.DataFrame, *, gap: float = 0.03,
                stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Large gap up → short, betting the opening pop mean-reverts intraday."""
    e = _eligible(panel)
    e = e[e["gap_pct"] >= gap]
    return _emit(e, -1, stop_pct)


def gap_down_fade(panel: pd.DataFrame, *, gap: float = 0.03,
                  stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Large gap down → long, betting the opening drop bounces intraday."""
    e = _eligible(panel)
    e = e[e["gap_pct"] <= -gap]
    return _emit(e, +1, stop_pct)


def prev_high_breakout(panel: pd.DataFrame, *, gap: float = 0.0,
                       stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Open at/above yesterday's high → long breakout continuation."""
    e = _eligible(panel)
    e = e[(e["open"] >= e["prev_high"]) & (e["gap_pct"] >= gap)]
    return _emit(e, +1, stop_pct)


def prev_low_breakdown(panel: pd.DataFrame, *, gap: float = 0.0,
                       stop_pct: float = DEFAULT_STOP_PCT) -> dict[date, list[Signal]]:
    """Open at/below yesterday's low → short breakdown continuation."""
    e = _eligible(panel)
    e = e[(e["open"] <= e["prev_low"]) & (e["gap_pct"] <= -gap)]
    return _emit(e, -1, stop_pct)


ALL_STRATEGIES = {
    "gap_up_momentum": gap_up_momentum,
    "gap_down_momentum": gap_down_momentum,
    "gap_up_fade": gap_up_fade,
    "gap_down_fade": gap_down_fade,
    "prev_high_breakout": prev_high_breakout,
    "prev_low_breakdown": prev_low_breakdown,
}
