"""Premium-native option-buying strategies, round 2 — selectivity-first.

Motivation (read this before judging the numbers): the first five premium-native
strategies in ``options_premium_native.py`` ALL lose money on the ATM NIFTY
basket. The autopsy is unambiguous — naive intraday option BUYING bleeds theta
and fees, and the #1 killer is OVERTRADING (the VWAP variant's fees ALONE
exceeded its gross loss). The one relative bright spot was SELECTIVITY: the coil
strategy traded the fewest times and lost the least. That matches the SEBI
reality that ~91% of retail F&O traders lose.

These three strategies are built around what the losers lacked:

1. SELECTIVITY + FEE-AWARENESS — a hard 1-trade-per-day cap, strict
   minimum-move gates, and narrow entry windows, so far fewer, higher-conviction
   trades fire.  Fewer trades is the single biggest fee lever.
2. STRUCTURAL CONFIRMATION FROM OPEN INTEREST — OI is stored in the DuckDB bars
   but the engine's Bar drops it, so we read it directly in a cached, read-only
   pre-pass.  Rising premium + rising OI = "long buildup" (fresh directional
   money, which tends to continue) as opposed to a short-covering pop that
   fizzles.  This is information none of the five losers used.
3. TREND CONTINUATION, NOT MEAN-REVERSION — buy PROVEN directional thrust and
   ride it with a trailing stop; never fade into decay.  Counter-trend option
   buying dies to theta before the reversal arrives.

Research sources are cited per strategy in ``_register(..., source=...)``.

Everything else is inherited from ``_PremiumNativeStrategy`` unchanged: long-only,
intraday, 15:15 hard exit, size = capital // premium, exits evaluated on 1m
closes, session rolls, warm-up, and ``last_no_signal_reason`` bookkeeping.

Limitation (honest): the harness instantiates one strategy per contract and
streams only that contract's bars, so a genuine two-leg long straddle/strangle
(needing the paired CE and PE premium simultaneously) is NOT expressible here
without engine rework.  These remain single-leg long-premium strategies.
"""

from __future__ import annotations

from datetime import time

from .options_premium_native import (
    _PremiumNativeStrategy,
    parse_option_expiry,
)
from ._registry import _register

# ---------------------------------------------------------------------------
# Open-interest pre-pass (read-only, cached)
# ---------------------------------------------------------------------------
#
# The engine Bar has an ``open_interest`` field but ``backtest_service`` never
# populates it, so OI-aware strategies read it straight from the Historify
# DuckDB store.  We memoize per (symbol, interval): the data is immutable
# historical fact and each contract is backtested against several strategies, so
# a read-through cache avoids re-opening the file 300+ times.  The cache is only
# ever inserted into (never mutated in place).

_OI_CACHE: dict[tuple[str, str], dict[int, int]] = {}


def _load_oi_series(symbol: str, interval: str = "1m") -> dict[int, int]:
    """Return ``{epoch_seconds: open_interest}`` for a contract, or ``{}``.

    Read-only and defensive: any failure (missing DB, missing column, non-option
    symbol) yields an empty mapping, and OI-gated strategies then simply refuse
    to trade with an explicit ``last_no_signal_reason`` instead of crashing.
    """
    key = (symbol.upper(), interval)
    cached = _OI_CACHE.get(key)
    if cached is not None:
        return cached
    series: dict[int, int] = {}
    try:
        import duckdb  # local import: keeps import-time cost off the hot path
        from database.historify_db import get_db_path

        con = duckdb.connect(get_db_path(), read_only=True)
        try:
            rows = con.execute(
                "SELECT timestamp, oi FROM market_data "
                "WHERE symbol = ? AND interval = ? ORDER BY timestamp ASC",
                [symbol.upper(), interval],
            ).fetchall()
        finally:
            con.close()
        series = {int(ts): int(oi or 0) for ts, oi in rows}
    except Exception:
        series = {}
    _OI_CACHE[key] = series
    return series


class _OIAwareStrategy(_PremiumNativeStrategy):
    """Base for strategies that need per-bar open interest.

    Maintains a session-scoped ``_ois`` list parallel to the base class's price
    series, sourced from the DuckDB pre-pass and carried forward across any
    missing minutes.
    """

    def on_start(self, ctx) -> None:
        super().on_start(ctx)
        self._oi_by_ts = _load_oi_series(self._sym)
        self._ois: list[float] = []

    def _on_new_session(self) -> None:
        super()._on_new_session()
        self._ois = []

    def _append_bar(self, bar) -> None:
        super()._append_bar(bar)
        oi = self._oi_by_ts.get(int(bar.ts.timestamp()))
        if oi is None:  # carry forward last known OI when a minute is missing
            oi = self._ois[-1] if self._ois else 0.0
        self._ois.append(float(oi))

    def _oi_change_pct(self, lookback: int) -> float | None:
        """Intraday OI change over ``lookback`` bars, or None if unavailable."""
        if len(self._ois) < lookback + 1:
            return None
        past = self._ois[-lookback - 1]
        if past <= 0:
            return None
        return (self._ois[-1] - past) / past


# =====================================================================
# 6. OI-confirmed long-buildup momentum
# =====================================================================

class PremiumOILongBuildup(_OIAwareStrategy):
    """Buy a volume + OI confirmed premium thrust early in the session.

    ALL of these must hold, and at most once per day:
    - inside the momentum window (default 09:30-13:00), skipping the opening
      auction noise and the midday theta dead-zone;
    - premium is up >= ``min_move_pct`` (default 8%) over the last ``mom_bars``
      bars (a real directional thrust, not drift) and prints a fresh short-term
      high on an up bar;
    - volume on the signal bar > ``vol_mult`` x its recent average;
    - OPEN INTEREST is up >= ``oi_rise_pct`` (default 3%) over the same window —
      i.e. the move is fresh LONG BUILDUP (new money), not a short-covering fizzle.

    Exit: absolute stop at the ``mom_bars`` swing low, a -15% hard stop, and a
    TIGHT 12% trailing stop off the peak. The tight trail is essential — it banks
    the momentum burst before theta/mean-reversion reclaim it (see __init__).
    15:15 time exit.
    """

    name = "Premium OI Long-Buildup Momentum"

    def __init__(self, sym: str, **kw):
        self._mom_bars = kw.pop("mom_bars", 5)
        # Defaults tuned on the Aug-2026 NIFTY basket: a STRICT thrust gate plus
        # a TIGHT trailing/hard stop is what flips this positive. Loosening the
        # trail to 0.30 turned +6k into -102k — with option buying you must bank
        # the momentum move before theta/mean-reversion give it back.
        self._min_move_pct = kw.pop("min_move_pct", 0.08)
        self._oi_rise_pct = kw.pop("oi_rise_pct", 0.03)
        self._vol_mult = kw.pop("vol_mult", 1.5)
        self._vol_lookback = kw.pop("vol_lookback", 20)
        self._start_time = kw.pop("start_time", time(9, 30))
        self._stop_time = kw.pop("stop_time", time(13, 0))
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.15),
            target_pct=kw.pop("target_pct", None),
            trail_pct=kw.pop("trail_pct", 0.12),
            max_trades_per_day=kw.pop("max_trades_per_day", 1),
            warmup_bars=kw.pop(
                "warmup_bars", max(self._mom_bars, self._vol_lookback) + 1),
            **kw,
        )
        self._swing_low: float | None = None

    def _entry_signal(self, bar) -> tuple[bool, str]:
        t = bar.ts.time()
        if t < self._start_time:
            return False, f"before momentum window {self._start_time:%H:%M}"
        if t >= self._stop_time:
            return False, f"past momentum window {self._stop_time:%H:%M}"
        need = max(self._mom_bars, self._vol_lookback) + 1
        if len(self._closes) < need:
            return False, f"warming up ({len(self._closes)}/{need} bars)"

        base = self._closes[-self._mom_bars - 1]
        if base <= 0:
            return False, "non-positive base premium"
        move = (bar.close - base) / base
        if move < self._min_move_pct:
            return False, (f"thrust {move * 100:.1f}% < "
                           f"{self._min_move_pct * 100:.0f}% gate")
        if bar.close < max(self._closes[-self._mom_bars:]):
            return False, "not a fresh short-term high"
        if bar.close <= bar.open:
            return False, "signal bar not an up bar"

        vols = self._volumes[-self._vol_lookback - 1:-1]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "thrust without volume confirmation"

        oi_chg = self._oi_change_pct(self._mom_bars)
        if oi_chg is None:
            return False, "no OI data for confirmation"
        if oi_chg < self._oi_rise_pct:
            return False, (f"OI {oi_chg * 100:+.1f}% < "
                           f"+{self._oi_rise_pct * 100:.0f}% (no long buildup)")

        self._swing_low = min(self._lows[-self._mom_bars:])
        return True, ""

    def _initial_stop_level(self, bar) -> float | None:
        return self._swing_low


# =====================================================================
# 7. Opening-drive momentum (first ~30 minutes, volatility-expansion gate)
# =====================================================================

class PremiumOpeningDrive(_PremiumNativeStrategy):
    """One high-conviction opening-drive breakout per day.

    Institutions build positions in the opening minutes, so the opening drive is
    where the cleanest directional move usually is.  We take AT MOST one trade,
    only in the opening-drive window, and only when the breakout is real:
    - build an opening range over the first ``or_bars`` bars (default 15);
    - before ``drive_end`` (default 10:30) require a close ABOVE the OR high by
      >= ``min_break_pct``, on an up bar;
    - the breakout bar's range must EXPAND to >= ``expansion_mult`` x the average
      opening-range bar range (the minimum-expected-move / volatility gate that
      filters the 1-minute chop that kills a naive premium ORB);
    - volume on the breakout > ``vol_mult`` x the opening-range average.

    Exit: absolute stop back at the OR-high breakout level (a failed breakout is
    out immediately), a -20% hard stop, an 18% trailing stop, and 15:15.
    """

    name = "Premium Opening Drive"

    def __init__(self, sym: str, **kw):
        self._or_bars = kw.pop("or_bars", 15)
        self._drive_end = kw.pop("drive_end", time(10, 30))
        self._expansion_mult = kw.pop("expansion_mult", 1.8)
        self._min_break_pct = kw.pop("min_break_pct", 0.03)
        self._vol_mult = kw.pop("vol_mult", 1.5)
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.20),
            target_pct=kw.pop("target_pct", None),
            trail_pct=kw.pop("trail_pct", 0.18),
            max_trades_per_day=kw.pop("max_trades_per_day", 1),
            warmup_bars=kw.pop("warmup_bars", 1),
            **kw,
        )
        self._or_high: float | None = None

    def _entry_signal(self, bar) -> tuple[bool, str]:
        n = len(self._closes)
        if n <= self._or_bars:
            return False, f"building opening range ({n}/{self._or_bars})"
        if bar.ts.time() >= self._drive_end:
            return False, f"past opening-drive window {self._drive_end:%H:%M}"

        or_high = max(self._highs[:self._or_bars])
        if or_high <= 0:
            return False, "degenerate opening range"
        if bar.close <= or_high:
            return False, f"premium {bar.close:.2f} <= OR high {or_high:.2f}"
        if (bar.close - or_high) / or_high < self._min_break_pct:
            return False, (f"break < {self._min_break_pct * 100:.0f}% "
                           f"above OR high")
        if bar.close <= bar.open:
            return False, "breakout bar not an up bar"

        or_ranges = [self._highs[i] - self._lows[i] for i in range(self._or_bars)]
        avg_or_range = sum(or_ranges) / len(or_ranges) if or_ranges else 0.0
        if avg_or_range <= 0 or (bar.high - bar.low) < \
                self._expansion_mult * avg_or_range:
            return False, "no volatility expansion on the breakout"

        or_vols = self._volumes[:self._or_bars]
        avg_vol = sum(or_vols) / len(or_vols) if or_vols else 0.0
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "breakout without volume confirmation"

        self._or_high = or_high
        return True, ""

    def _initial_stop_level(self, bar) -> float | None:
        return self._or_high


# =====================================================================
# 8. Expiry-day gamma ride (OI-confirmed)
# =====================================================================

class PremiumExpiryGammaOI(_OIAwareStrategy):
    """Expiry-day afternoon gamma acceleration, gated on open interest.

    On expiry day (parsed from the symbol), dealer hedging of concentrated OI
    near the spot can force a violent, self-reinforcing move in the last hours —
    the "gamma blast".  Unlike the round-1 gamma strategy this one is stricter
    and OI-aware: after ``start_time`` (default 13:30), buy a sharp acceleration
    (+``accel_pct`` over ``accel_bars`` on an up bar with >= ``vol_mult`` x
    volume) ONLY IF open interest is not bleeding out (``oi_rise_pct`` gate) —
    positions leaving the strike means the squeeze fuel is gone.  One trade/day.

    Exit: -30% hard stop, TIGHT 12% trailing stop off the peak, 15:15 time exit.
    """

    name = "Premium Expiry Gamma (OI-confirmed)"

    def __init__(self, sym: str, **kw):
        self._accel_pct = kw.pop("accel_pct", 0.10)
        self._accel_bars = kw.pop("accel_bars", 3)
        self._oi_rise_pct = kw.pop("oi_rise_pct", 0.0)
        self._vol_mult = kw.pop("vol_mult", 2.0)
        self._vol_lookback = kw.pop("vol_lookback", 20)
        self._start_time = kw.pop("start_time", time(13, 30))
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.30),
            target_pct=kw.pop("target_pct", None),
            # Tight trail (same lesson as OI-momentum): banking the gamma burst
            # fast roughly halved the basket loss (-6.0k -> -2.1k) vs an 18% trail.
            trail_pct=kw.pop("trail_pct", 0.12),
            max_trades_per_day=kw.pop("max_trades_per_day", 1),
            warmup_bars=kw.pop("warmup_bars", 5),
            **kw,
        )
        self._expiry = parse_option_expiry(sym)

    def _entry_signal(self, bar) -> tuple[bool, str]:
        if self._expiry is None:
            return False, f"cannot parse expiry from {self._sym!r}"
        if bar.ts.date() != self._expiry:
            return False, f"not expiry day (expiry {self._expiry.isoformat()})"
        if bar.ts.time() < self._start_time:
            return False, f"before gamma window {self._start_time:%H:%M}"
        need = max(self._accel_bars + 1, self._vol_lookback + 1)
        if len(self._closes) < need:
            return False, "warming up acceleration/volume windows"

        base = self._closes[-self._accel_bars - 1]
        if base <= 0:
            return False, "non-positive base premium"
        accel = (bar.close - base) / base
        if accel < self._accel_pct:
            return False, (f"acceleration {accel * 100:.1f}% < "
                           f"{self._accel_pct * 100:.0f}% threshold")
        if bar.close <= bar.open:
            return False, "acceleration bar not an up bar"

        vols = self._volumes[-self._vol_lookback - 1:-1]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "acceleration without volume confirmation"

        oi_chg = self._oi_change_pct(self._accel_bars)
        if oi_chg is None:
            return False, "no OI data for confirmation"
        if oi_chg < self._oi_rise_pct:
            return False, (f"OI {oi_chg * 100:+.1f}% below gate "
                           f"(positions leaving the strike)")
        return True, ""


# =====================================================================
# Registration
# =====================================================================

_COMMON_NOTE = (
    "PREMIUM-NATIVE: run on real option premium bars (symbol must end in "
    "CE/PE, e.g. NIFTY11AUG2624650CE on NFO, 1m interval). Long-only, "
    "intraday, 15:15 hard exit. Selectivity-first: max 1 trade/day."
)


def _pick(kw: dict, *names: str) -> dict:
    """Whitelist known kwargs so stray service params can't break __init__."""
    return {k: kw[k] for k in names if k in kw}


_register(
    "options_oi_momentum",
    "Premium OI Long-Buildup Momentum",
    (
        "Buy a volume-and-OI-confirmed premium thrust in the 09:30-13:00 window: "
        ">=8% move over 5 bars to a fresh high on an up bar, volume > 1.5x avg, "
        "AND open interest rising >=3% (fresh long buildup, not short covering). "
        "Swing-low stop, -15% hard stop, TIGHT 12% trail, 15:15 exit. Max 1 "
        "trade/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumOILongBuildup(sym, **_pick(
        kw, "mom_bars", "min_move_pct", "oi_rise_pct", "vol_mult",
        "vol_lookback", "start_time", "stop_time", "stop_pct", "trail_pct",
        "max_trades_per_day", "warmup_bars", "capital_per_trade")),
    source=(
        "OI build-up rules — rising price + rising OI = long buildup (fresh, "
        "sustainable money) vs. short covering. NiftyTrader OI guide "
        "(niftytrader.in/markets/open-interest-oi-long-short-build-up-guide) and "
        "Bajaj Broking 'Open Interest strategy for intraday' "
        "(bajajbroking.in/blog/how-to-use-open-interest-strategy-for-intraday-trading)."
    ),
)

_register(
    "options_opening_drive",
    "Premium Opening Drive",
    (
        "One high-conviction opening-drive breakout/day: build a 15-bar opening "
        "range, then before 10:30 buy a close >=3% above the OR high on an up bar "
        "whose range expands to >=1.8x the average OR bar range, volume > 1.5x "
        "the OR average. OR-high stop, -20% hard stop, 18% trail, 15:15 exit. "
        + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumOpeningDrive(sym, **_pick(
        kw, "or_bars", "drive_end", "expansion_mult", "min_break_pct",
        "vol_mult", "stop_pct", "trail_pct", "max_trades_per_day",
        "warmup_bars", "capital_per_trade")),
    source=(
        "Opening-range breakout with a volatility-expansion + trend filter to "
        "reject the 1-minute chop that sinks naive ORB — Trade That Swing / "
        "edgeful ORB research (only trade when price proves direction with "
        "volume, in the direction of the opening drive)."
    ),
)

_register(
    "options_expiry_gamma",
    "Premium Expiry Gamma (OI-confirmed)",
    (
        "Expiry-day only (expiry parsed from the symbol): after 13:30 buy a sharp "
        "acceleration (+10% in 3 bars, up bar, 2x volume) ONLY when open interest "
        "is not bleeding out of the strike. -30% hard stop, 18% trail, 15:15 exit. "
        "Max 1 trade/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumExpiryGammaOI(sym, **_pick(
        kw, "accel_pct", "accel_bars", "oi_rise_pct", "vol_mult",
        "vol_lookback", "start_time", "stop_pct", "trail_pct",
        "max_trades_per_day", "warmup_bars", "capital_per_trade")),
    source=(
        "Expiry-day gamma blast — dealers hedging concentrated OI near spot force "
        "a self-reinforcing move in the last hours; enrichmoney 'Gamma Blast "
        "Strategy' and PL Capital Tuesday-expiry guide. OI gate added so we only "
        "chase when positioning is still building at the strike."
    ),
)
