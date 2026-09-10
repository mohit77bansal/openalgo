"""Premium-native SCALPING strategies on 1-minute option premium bars — round 2.

The original ``options_scalping.py`` suite scalps an ATR/underlying PROXY
premium and is WRONG on real premium bars (it reconstructs the premium from
underlying moves via a delta-0.5 estimate, but on a real CE/PE series
``bar.close`` already IS the premium).  These three strategies instead
subclass ``_PremiumNativeStrategy`` and read/trade the real premium series
directly — the same, correct plumbing used by ``options_premium_native.py``.

Scalping profile (what makes these "scalps" rather than the day-swing premium
strategies): tiny per-trade edges, tight TP/SL on premium, a hard few-bar
time stop, and a higher daily trade cap.  This is the regime where FEES
dominate, so every result MUST be read net-of-fees and under the conservative
fee stress (see ``scripts/scalping_basket_backtest.py``).

Three approaches, each expressible on 1m OHLCV(+volume):

1. Momentum burst  (``options_scalp_momo``)   — ride a fast premium impulse,
   bank it in a few bars.  Trend/continuation.
2. VWAP reversion  (``options_scalp_revert``) — buy a premium stretched below
   its intraday VWAP as it turns up, exit on the snap back.  Mean-reversion.
3. Micro-range break (``options_scalp_range``) — buy a volume-confirmed break
   of a tight 6-bar premium consolidation.  Breakout.

Honesty rules inherited unchanged from ``_PremiumNativeStrategy``: long-only
(option BUYING, max loss = premium + fees), intraday with a 15:15 hard exit,
size = capital_per_trade // entry premium, exits evaluated on 1m closes, and
``last_no_signal_reason`` bookkeeping so a zero-trade run is explainable.
"""

from __future__ import annotations

from datetime import time

from .options_premium_native import _PremiumNativeStrategy
from ._registry import _register

# -- Constants ----------------------------------------------------------------

# Scalps do many trades, so entries stop a little earlier and the time exit is
# unchanged; the value comes from the tight bar-count stop, not the day close.
_SCALP_ENTRY_CUTOFF = time(15, 0)


def _rolling_avg(values: list[float], lookback: int) -> float:
    """Average of the ``lookback`` values BEFORE the current one (exclusive)."""
    window = values[-lookback - 1:-1]
    return sum(window) / len(window) if window else 0.0


class _ScalpBase(_PremiumNativeStrategy):
    """Adds a hard bar-count time stop on top of the premium-native exit ladder.

    A scalp must not sit in decay: if neither the tight target nor the tight
    stop triggers within ``max_hold_bars`` bars, we flatten.  Implemented via
    the base class's ``_extra_exit_reason`` hook so the rest of the exit ladder
    (time exit, absolute stop, hard stop, target, trail) is reused verbatim.
    """

    def __init__(self, sym: str, *, max_hold_bars: int, **kw):
        self._max_hold_bars = max_hold_bars
        super().__init__(sym, **kw)

    def _bars_held(self) -> int:
        if self._pos is None:
            return 0
        return (len(self._closes) - 1) - self._pos.entry_index

    def _scalp_time_stop(self) -> str | None:
        if self._max_hold_bars > 0 and self._bars_held() >= self._max_hold_bars:
            return f"scalp-time-stop-{self._max_hold_bars}bar"
        return None

    def _extra_exit_reason(self, bar) -> str | None:
        return self._scalp_time_stop()


# =====================================================================
# 1. Momentum burst scalp
# =====================================================================

class ScalpMomentumBurst(_ScalpBase):
    """Buy a fast premium impulse and bank it within a few bars.

    Entry (all must hold):
      - inside the intraday window (default 09:25-15:00), past the opening auction;
      - premium up >= ``burst_pct`` (default 3%) over the last ``burst_bars``
        (default 2) bars — a real impulse, not drift;
      - the signal bar is an up bar making a fresh ``burst_bars``-bar high;
      - volume on the signal bar > ``vol_mult`` x its short recent average.

    Exit: +``target_pct`` (default 5%) target, -``stop_pct`` (default 3%) hard
    stop, ``max_hold_bars`` (default 3) bar time stop, 15:15.  No mean-reversion
    fade — this only ever buys proven upward premium thrust.
    """

    name = "Scalp Momentum Burst"

    def __init__(self, sym: str, **kw):
        self._burst_bars = kw.pop("burst_bars", 2)
        self._burst_pct = kw.pop("burst_pct", 0.03)
        self._vol_mult = kw.pop("vol_mult", 1.3)
        self._vol_lookback = kw.pop("vol_lookback", 10)
        self._start_time = kw.pop("start_time", time(9, 25))
        super().__init__(
            sym,
            max_hold_bars=kw.pop("max_hold_bars", 3),
            stop_pct=kw.pop("stop_pct", 0.03),
            target_pct=kw.pop("target_pct", 0.05),
            trail_pct=kw.pop("trail_pct", None),
            max_trades_per_day=kw.pop("max_trades_per_day", 10),
            warmup_bars=kw.pop("warmup_bars", max(self._burst_bars, self._vol_lookback) + 1),
            entry_cutoff=kw.pop("entry_cutoff", _SCALP_ENTRY_CUTOFF),
            **kw,
        )

    def _entry_signal(self, bar) -> tuple[bool, str]:
        if bar.ts.time() < self._start_time:
            return False, f"before scalp window {self._start_time:%H:%M}"
        need = max(self._burst_bars, self._vol_lookback) + 1
        if len(self._closes) < need:
            return False, f"warming up ({len(self._closes)}/{need} bars)"

        base = self._closes[-self._burst_bars - 1]
        if base <= 0:
            return False, "non-positive base premium"
        burst = (bar.close - base) / base
        if burst < self._burst_pct:
            return False, (f"burst {burst * 100:.1f}% < "
                           f"{self._burst_pct * 100:.0f}% gate")
        if bar.close <= bar.open:
            return False, "signal bar not an up bar"
        if bar.close < max(self._closes[-self._burst_bars:]):
            return False, "not a fresh short-term high"

        avg_vol = _rolling_avg(self._volumes, self._vol_lookback)
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "burst without volume confirmation"
        return True, ""


# =====================================================================
# 2. VWAP reversion scalp
# =====================================================================

class ScalpVwapReversion(_ScalpBase):
    """Buy a premium stretched below intraday VWAP as it turns up.

    VWAP is cumulative typical-price x volume on the PREMIUM series from 09:15.

    Entry (all must hold):
      - at least ``min_session_bars`` (default 6) bars into the session so VWAP
        is meaningful, and before the entry cutoff;
      - premium is stretched >= ``stretch_pct`` (default 1.5%) BELOW VWAP;
      - the signal bar turns up (close > open AND close > previous close) — the
        reversion trigger, so we do not knife-catch a bleed.

    Exit: snap back to/above VWAP (``vwap-touch``), +``target_pct`` (default 5%)
    cap, -``stop_pct`` (default 3%) hard stop, ``max_hold_bars`` (default 5) bar
    time stop, 15:15.  Counter-trend option buying is theta-hostile, so the
    hold is deliberately short — this is the strategy most exposed to the
    "fades die to decay" failure mode, and is included to TEST that honestly.
    """

    name = "Scalp VWAP Reversion"

    def __init__(self, sym: str, **kw):
        self._stretch_pct = kw.pop("stretch_pct", 0.015)
        self._min_session_bars = kw.pop("min_session_bars", 6)
        super().__init__(
            sym,
            max_hold_bars=kw.pop("max_hold_bars", 5),
            stop_pct=kw.pop("stop_pct", 0.03),
            target_pct=kw.pop("target_pct", 0.05),
            trail_pct=kw.pop("trail_pct", None),
            max_trades_per_day=kw.pop("max_trades_per_day", 8),
            warmup_bars=kw.pop("warmup_bars", self._min_session_bars),
            entry_cutoff=kw.pop("entry_cutoff", _SCALP_ENTRY_CUTOFF),
            **kw,
        )
        self._vwaps: list[float] = []

    def _on_new_session(self) -> None:
        self._vwaps = []
        self._cum_pv = 0.0
        self._cum_v = 0.0

    def _append_bar(self, bar) -> None:
        super()._append_bar(bar)
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = self._volumes[-1]
        self._cum_pv += typical * vol
        self._cum_v += vol
        self._vwaps.append(self._cum_pv / self._cum_v if self._cum_v > 0 else bar.close)

    def _entry_signal(self, bar) -> tuple[bool, str]:
        if len(self._closes) < max(self._min_session_bars, 2):
            return False, f"warming up VWAP ({len(self._closes)}/{self._min_session_bars})"
        vwap = self._vwaps[-1]
        if vwap <= 0:
            return False, "non-positive VWAP"
        stretch = (vwap - bar.close) / vwap
        if stretch < self._stretch_pct:
            return False, (f"only {stretch * 100:.1f}% below VWAP "
                           f"(< {self._stretch_pct * 100:.1f}% gate)")
        if not (bar.close > bar.open and bar.close > self._closes[-2]):
            return False, "no upturn trigger on the stretched bar"
        return True, ""

    def _extra_exit_reason(self, bar) -> str | None:
        if self._vwaps and bar.close >= self._vwaps[-1]:
            return "vwap-touch"
        return self._scalp_time_stop()


# =====================================================================
# 3. Micro-range breakout scalp
# =====================================================================

class ScalpMicroRange(_ScalpBase):
    """Buy a volume-confirmed break of a tight premium micro-consolidation.

    Entry (all must hold), re-arming after every exit:
      - a coiled ``range_bars`` (default 6) window: (high-low)/ref premium
        <= ``range_pct`` (default 2.5%);
      - the current close breaks the window high by >= ``break_pct`` (default 1%)
        on an up bar;
      - volume on the breakout > ``vol_mult`` x the window average.

    Exit: absolute stop at the micro-range low, +``target_pct`` (default 6%)
    target, -``stop_pct`` (default 4%) hard stop, ``max_hold_bars`` (default 4)
    bar time stop, 15:15.
    """

    name = "Scalp Micro-Range Break"

    def __init__(self, sym: str, **kw):
        self._range_bars = kw.pop("range_bars", 6)
        self._range_pct = kw.pop("range_pct", 0.025)
        self._break_pct = kw.pop("break_pct", 0.01)
        self._vol_mult = kw.pop("vol_mult", 1.5)
        super().__init__(
            sym,
            max_hold_bars=kw.pop("max_hold_bars", 4),
            stop_pct=kw.pop("stop_pct", 0.04),
            target_pct=kw.pop("target_pct", 0.06),
            trail_pct=kw.pop("trail_pct", None),
            max_trades_per_day=kw.pop("max_trades_per_day", 8),
            warmup_bars=kw.pop("warmup_bars", self._range_bars + 1),
            entry_cutoff=kw.pop("entry_cutoff", _SCALP_ENTRY_CUTOFF),
            **kw,
        )
        self._range_low: float | None = None

    def _window(self) -> tuple[float, float] | None:
        """(range_high, range_low) over the N bars BEFORE the current bar."""
        if len(self._closes) < self._range_bars + 1:
            return None
        highs = self._highs[-self._range_bars - 1:-1]
        lows = self._lows[-self._range_bars - 1:-1]
        return max(highs), min(lows)

    def _entry_signal(self, bar) -> tuple[bool, str]:
        window = self._window()
        if window is None:
            return False, (f"warming up range window "
                           f"({len(self._closes)}/{self._range_bars + 1})")
        range_high, range_low = window
        ref = self._closes[-2]
        if ref <= 0:
            return False, "non-positive reference premium"
        if (range_high - range_low) / ref > self._range_pct:
            return False, "no coil: micro-range too wide"
        if bar.close <= range_high:
            return False, "coiled but no break of range high"
        if (bar.close - range_high) / range_high < self._break_pct:
            return False, f"break < {self._break_pct * 100:.0f}% above range high"
        if bar.close <= bar.open:
            return False, "breakout bar not an up bar"
        avg_vol = _rolling_avg(self._volumes, self._range_bars)
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "break without volume confirmation"
        self._range_low = range_low
        return True, ""

    def _initial_stop_level(self, bar) -> float | None:
        return self._range_low


# =====================================================================
# Registration
# =====================================================================

_COMMON_NOTE = (
    "PREMIUM-NATIVE SCALP: run on real option premium bars (symbol must end in "
    "CE/PE, e.g. NIFTY18AUG2624500CE on NFO, 1m interval). Long-only, intraday, "
    "tight TP/SL + few-bar time stop, 15:15 hard exit. FEES DOMINATE — read "
    "net-of-fees and under the conservative fee stress."
)


def _pick(kw: dict, *names: str) -> dict:
    """Whitelist known kwargs so stray service params can't break __init__."""
    return {k: kw[k] for k in names if k in kw}


_register(
    "options_scalp_momo",
    "Scalp Momentum Burst",
    (
        "Buy a fast premium impulse (>=3% over 2 bars to a fresh high on an up "
        "bar, volume > 1.3x its 10-bar average) in the 09:25-15:00 window and "
        "bank it: +5% target, -3% hard stop, 3-bar time stop, 15:15 exit. Max "
        "10 trades/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: ScalpMomentumBurst(sym, **_pick(
        kw, "burst_bars", "burst_pct", "vol_mult", "vol_lookback", "start_time",
        "max_hold_bars", "stop_pct", "target_pct", "trail_pct",
        "max_trades_per_day", "warmup_bars", "capital_per_trade")),
    source="Fast premium momentum-continuation scalp; bank the burst before decay.",
)

_register(
    "options_scalp_revert",
    "Scalp VWAP Reversion",
    (
        "Buy a premium stretched >=1.5% below its intraday VWAP as the bar turns "
        "up; exit on the snap back to VWAP, +5% cap, -3% hard stop, 5-bar time "
        "stop, 15:15. Max 8 trades/day. Counter-trend and theta-exposed — "
        "included to test the fade honestly. " + _COMMON_NOTE
    ),
    lambda sym, **kw: ScalpVwapReversion(sym, **_pick(
        kw, "stretch_pct", "min_session_bars", "max_hold_bars", "stop_pct",
        "target_pct", "trail_pct", "max_trades_per_day", "warmup_bars",
        "capital_per_trade")),
    source="Intraday VWAP mean-reversion scalp applied directly to the premium.",
)

_register(
    "options_scalp_range",
    "Scalp Micro-Range Break",
    (
        "Buy a volume-confirmed break (>=1% on an up bar, volume > 1.5x average) "
        "of a tight 6-bar premium consolidation (range <=2.5% of premium). Stop "
        "at the micro-range low / -4%, +6% target, 4-bar time stop, 15:15. Max 8 "
        "trades/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: ScalpMicroRange(sym, **_pick(
        kw, "range_bars", "range_pct", "break_pct", "vol_mult", "max_hold_bars",
        "stop_pct", "target_pct", "trail_pct", "max_trades_per_day",
        "warmup_bars", "capital_per_trade")),
    source="Volatility-contraction breakout scalp on the premium series itself.",
)
