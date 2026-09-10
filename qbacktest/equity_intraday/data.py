"""Load cached bhavcopy CSVs into a feature panel for intraday screening.

Produces one tidy DataFrame — one row per (symbol, date) — with the daily OHLCV
plus the rolling features a same-day intraday screener needs:

    gap_pct        (open - prev_close) / prev_close      — overnight gap
    range_pct      (high - low) / prev_close             — realised day range
    ret_cc         (close - prev_close) / prev_close     — close-to-close return
    ret_oc         (close - open) / open                 — the intraday move we trade
    avg_turnover20 20-day mean turnover (₹ lacs)         — liquidity filter
    rel_volume     volume / 20-day mean volume           — "in play" today
    atr20_pct      20-day mean range_pct                 — typical volatility
    prev_high/low  yesterday's high / low                — breakout references

All features at row t use only data available at/through t (prev_* are shifted),
so a same-day screen that keys off gap_pct / prev_high has no look-ahead: those
are known at 09:15 before entry. Features that would peek (today's close) are
only ever used as the *exit*, never the signal.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE_DIR = Path("data/bhavcopy_eq")

_COLS = {
    "SYMBOL": "symbol",
    "SERIES": "series",
    "PREV_CLOSE": "prev_close",
    "OPEN_PRICE": "open",
    "HIGH_PRICE": "high",
    "LOW_PRICE": "low",
    "CLOSE_PRICE": "close",
    "TTL_TRD_QNTY": "volume",
    "TURNOVER_LACS": "turnover",
    "DELIV_PER": "deliv_per",
}


def _load_one(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(path, skipinitialspace=True)
    except Exception:
        return None
    df.columns = [c.strip().upper() for c in df.columns]
    if "SERIES" not in df.columns or "SYMBOL" not in df.columns:
        return None
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df = df[df["SERIES"].isin(["EQ", "BE"])].copy()
    keep = {k: v for k, v in _COLS.items() if k in df.columns}
    df = df[list(keep)].rename(columns=keep)
    df["date"] = pd.to_datetime(path.stem).date()
    for c in ("prev_close", "open", "high", "low", "close", "volume", "turnover", "deliv_per"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    return df


def load_panel(cache_dir: Path = CACHE_DIR) -> pd.DataFrame:
    """Concatenate all cached bhavcopy days into one feature panel."""
    files = sorted(cache_dir.glob("*.csv"))
    frames = [d for d in (_load_one(f) for f in files) if d is not None and not d.empty]
    if not frames:
        raise RuntimeError(f"no bhavcopy CSVs in {cache_dir}")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["open", "high", "low", "close", "prev_close"])
    panel = panel[(panel["prev_close"] > 0) & (panel["open"] > 0)]
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)

    g = panel.groupby("symbol", group_keys=False)

    panel["gap_pct"] = (panel["open"] - panel["prev_close"]) / panel["prev_close"]
    panel["range_pct"] = (panel["high"] - panel["low"]) / panel["prev_close"]
    panel["ret_cc"] = (panel["close"] - panel["prev_close"]) / panel["prev_close"]
    panel["ret_oc"] = (panel["close"] - panel["open"]) / panel["open"]

    # Rolling features use *prior* rows only (shift(1)) → known before today's open.
    panel["avg_turnover20"] = g["turnover"].apply(
        lambda s: s.shift(1).rolling(20, min_periods=10).mean()
    )
    panel["avg_volume20"] = g["volume"].apply(
        lambda s: s.shift(1).rolling(20, min_periods=10).mean()
    )
    panel["atr20_pct"] = g["range_pct"].apply(
        lambda s: s.shift(1).rolling(20, min_periods=10).mean()
    )
    panel["prev_high"] = g["high"].shift(1)
    panel["prev_low"] = g["low"].shift(1)
    panel["prev_ret_cc"] = g["ret_cc"].shift(1)

    panel["rel_volume"] = panel["volume"] / panel["avg_volume20"]
    return panel


def trading_days(panel: pd.DataFrame) -> list:
    return sorted(panel["date"].unique())
