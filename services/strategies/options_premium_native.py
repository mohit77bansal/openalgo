"""Premium-native option-buying strategies on 1-minute option premium bars.

Unlike the rest of the options suite (which trades an ATR/Black-Scholes
premium PROXY derived from underlying bars), these five strategies consume
REAL option premium OHLCV bars: the backtest symbol IS the option contract
(e.g. ``NIFTY11AUG2624650CE``) and open/high/low/close are premium prices
collected from the broker.  ``backtest_service`` skips the BS transform for
symbols ending in CE/PE and pins ``lot_size=1``, so the strategy reads and
trades the premium series directly.

Shared honesty rules:
- Long-only (option BUYING).  Max loss per trade = premium paid + fees.
- Intraday only: hard time exit (default 15:15 IST), no entries after the
  entry cutoff (default 15:00), defensive square-off on session change.
- Position size = ``capital_per_trade // entry premium`` whole units
  (premium bars carry lot_size=1; ~75 units approximates one NIFTY lot).
- Stops/targets/trails are evaluated on 1m bar CLOSES, matching the
  engine's fill-at-close convention.  No intrabar stop fills are modeled.
- Warm-up and skipped bars record ``self.last_no_signal_reason`` so a
  zero-trade run is explainable instead of silent.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date, time

from qbacktest.engine.strategy import Strategy

from ._registry import _register

# -- Constants ----------------------------------------------------------------

_DEFAULT_CAPITAL_PER_TRADE = 10_000.0
_DEFAULT_ENTRY_CUTOFF = time(15, 0)
_DEFAULT_TIME_EXIT = time(15, 15)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# NIFTY11AUG2624650CE -> underlying=NIFTY dd=11 mon=AUG yy=26 strike=24650 CE
_OPTION_SYMBOL_RE = re.compile(
    r"^([A-Z&\-]+?)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def _parse_contract(symbol: str):
    """Parse a full option contract symbol into (underlying, expiry, strike, type).

    Returns None when the symbol is not a well-formed option contract.
    """
    m = _OPTION_SYMBOL_RE.match((symbol or "").upper())
    if not m:
        return None
    underlying, dd, mon, yy, strike, opt_type = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        expiry = date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None
    return underlying, expiry, float(strike), opt_type


def parse_option_expiry(symbol: str) -> date | None:
    """Parse the expiry date from an OpenAlgo option symbol, or None."""
    parsed = _parse_contract(symbol)
    return parsed[1] if parsed else None


@dataclass(frozen=True)
class _OpenPosition:
    """Immutable record of the open long-premium position."""
    entry_price: float
    quantity: int
    entry_index: int          # index into the session bar arrays at entry
    stop_level: float | None  # absolute premium stop (e.g. OR mid, coil low)
    peak: float               # highest close since entry (for trailing)


# =====================================================================
# Base class — session tracking, sizing, and the shared exit ladder
# =====================================================================

class _PremiumNativeStrategy(Strategy):
    """Shared plumbing for premium-native option buying.

    Subclasses implement ``_entry_signal(bar) -> tuple[bool, str]`` and may
    override ``_extra_exit_reason(bar) -> str | None`` for custom exits.
    """

    name = "Premium Native Base"

    def __init__(
        self,
        sym: str,
        *,
        stop_pct: float | None,
        target_pct: float | None,
        trail_pct: float | None = None,
        max_trades_per_day: int = 3,
        warmup_bars: int = 5,
        entry_cutoff: time = _DEFAULT_ENTRY_CUTOFF,
        time_exit: time = _DEFAULT_TIME_EXIT,
        capital_per_trade: float = _DEFAULT_CAPITAL_PER_TRADE,
    ):
        self._sym = sym
        # Parsed contract identity (underlying/expiry/strike/type). Used to
        # match engine bars whose Instrument is canonical (symbol=underlying,
        # with expiry/strike/type as separate fields) rather than carrying the
        # full contract string. None when sym is not a well-formed contract.
        self._contract = _parse_contract(sym)
        self._stop_pct = stop_pct
        self._target_pct = target_pct
        self._trail_pct = trail_pct
        self._max_trades = max_trades_per_day
        self._warmup_bars = warmup_bars
        self._entry_cutoff = entry_cutoff
        self._time_exit = time_exit
        self._capital = capital_per_trade

    # -- Lifecycle --------------------------------------------------------

    def on_start(self, ctx):
        # Session-scoped premium series (reset each trading day).
        self._opens: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._volumes: list[float] = []

        self._session_date = None
        self._pos: _OpenPosition | None = None
        self._trades_today = 0
        self.last_no_signal_reason = ""

    def _matches_instrument(self, inst) -> bool:
        """True when ``inst`` is the contract this strategy trades.

        Handles both bar shapes: the canonical engine Instrument
        (``symbol`` = underlying, with ``expiry``/``strike``/type separate) and
        any path where ``symbol`` carries the full contract string.
        """
        if getattr(inst, "symbol", None) == self._sym:
            return True
        if self._contract is None:
            return False
        underlying, expiry, strike, opt_type = self._contract
        itype = getattr(inst, "instrument_type", None)
        itype_val = getattr(itype, "value", itype)
        return (
            getattr(inst, "symbol", None) == underlying
            and getattr(inst, "expiry", None) == expiry
            and getattr(inst, "strike", None) == strike
            and itype_val == opt_type
        )

    def on_bar(self, ctx, bar):
        if not self._matches_instrument(bar.instrument):
            return

        self._roll_session(ctx, bar)
        self._append_bar(bar)

        if self._pos is not None:
            self._manage_exit(ctx, bar)
            return

        blocked = self._entry_blocked_reason(bar)
        if blocked:
            self.last_no_signal_reason = blocked
            return

        should_enter, reason = self._entry_signal(bar)
        if not should_enter:
            self.last_no_signal_reason = reason
            return
        self._enter(ctx, bar)

    # -- Hooks for subclasses ----------------------------------------------

    def _entry_signal(self, bar) -> tuple[bool, str]:
        """Return (enter, no_signal_reason)."""
        return False, "base class has no entry signal"

    def _extra_exit_reason(self, bar) -> str | None:
        """Strategy-specific exit condition (e.g. VWAP cross-down)."""
        return None

    def _initial_stop_level(self, bar) -> float | None:
        """Absolute premium stop set at entry (None = percentage stop only)."""
        return None

    def _on_new_session(self) -> None:
        """Extra per-day reset for subclasses."""

    # -- Session / series management ---------------------------------------

    def _roll_session(self, ctx, bar) -> None:
        bar_date = bar.ts.date()
        if bar_date == self._session_date:
            return
        # Defensive: never carry a position overnight (gaps in the data
        # could skip the 15:15 exit bar).
        if self._pos is not None:
            ctx.sell(bar.instrument, quantity=self._pos.quantity,
                     tag="overnight-guard")
            self._pos = None
        self._session_date = bar_date
        self._opens, self._highs = [], []
        self._lows, self._closes, self._volumes = [], [], []
        self._trades_today = 0
        self._on_new_session()

    def _append_bar(self, bar) -> None:
        self._opens.append(bar.open)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._volumes.append(float(getattr(bar, "volume", 0.0) or 0.0))

    # -- Entry -------------------------------------------------------------

    def _entry_blocked_reason(self, bar) -> str:
        if len(self._closes) < self._warmup_bars:
            return (f"warming up: {len(self._closes)}/{self._warmup_bars} "
                    f"session bars")
        if bar.ts.time() >= self._entry_cutoff:
            return f"past entry cutoff {self._entry_cutoff:%H:%M}"
        if self._trades_today >= self._max_trades:
            return f"daily trade cap reached ({self._max_trades})"
        return ""

    def _enter(self, ctx, bar) -> None:
        premium = bar.close
        if premium <= 0:
            self.last_no_signal_reason = "non-positive premium"
            return
        quantity = int(self._capital // premium)
        if quantity < 1:
            self.last_no_signal_reason = (
                f"premium {premium:.2f} exceeds per-trade capital "
                f"{self._capital:.0f}")
            return
        ctx.buy(bar.instrument, quantity=quantity, tag=self.name)
        self._pos = _OpenPosition(
            entry_price=premium,
            quantity=quantity,
            entry_index=len(self._closes) - 1,
            stop_level=self._initial_stop_level(bar),
            peak=premium,
        )
        self._trades_today += 1
        self.last_no_signal_reason = ""

    # -- Exit ladder ---------------------------------------------------------

    def _manage_exit(self, ctx, bar) -> None:
        pos = self._pos
        if pos is None:
            return
        close = bar.close
        if close > pos.peak:
            pos = dataclasses.replace(pos, peak=close)
            self._pos = pos

        reason = self._exit_reason(pos, bar, close)
        if reason:
            ctx.sell(bar.instrument, quantity=pos.quantity, tag=reason)
            self._pos = None

    def _exit_reason(self, pos: _OpenPosition, bar, close: float) -> str | None:
        if bar.ts.time() >= self._time_exit:
            return f"time-exit-{self._time_exit:%H%M}"
        if pos.stop_level is not None and close <= pos.stop_level:
            return "absolute-stop"
        if self._stop_pct is not None and \
                close <= pos.entry_price * (1.0 - self._stop_pct):
            return "hard-stop"
        if self._target_pct is not None and \
                close >= pos.entry_price * (1.0 + self._target_pct):
            return "target"
        if self._trail_pct is not None and pos.peak > pos.entry_price and \
                close <= pos.peak * (1.0 - self._trail_pct):
            return "trailing-stop"
        return self._extra_exit_reason(bar)


# =====================================================================
# 1. Premium VWAP crossover
# =====================================================================

class PremiumVwapCross(_PremiumNativeStrategy):
    """Buy when the premium crosses above its intraday VWAP on rising volume.

    VWAP is computed on the PREMIUM series (typical price x volume,
    cumulative from 9:15).  Exit on cross back below VWAP, -20% premium
    stop, +40% target, or 15:15 time exit.
    """

    name = "Premium VWAP Cross"

    def __init__(self, sym: str, **kw):
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.20),
            target_pct=kw.pop("target_pct", 0.40),
            max_trades_per_day=kw.pop("max_trades_per_day", 3),
            warmup_bars=kw.pop("warmup_bars", 5),
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
        if len(self._closes) < 2:
            return False, "need 2 session bars for a cross"
        prev_close, close = self._closes[-2], self._closes[-1]
        prev_vwap, vwap = self._vwaps[-2], self._vwaps[-1]
        if not (prev_close <= prev_vwap and close > vwap):
            return False, "no upward VWAP cross"
        if self._volumes[-1] <= 0 or self._volumes[-1] <= self._volumes[-2]:
            return False, "volume not rising on the cross"
        return True, ""

    def _extra_exit_reason(self, bar) -> str | None:
        if self._vwaps and bar.close < self._vwaps[-1]:
            return "vwap-cross-down"
        return None


# =====================================================================
# 2. Premium opening-range breakout
# =====================================================================

class PremiumORB(_PremiumNativeStrategy):
    """Opening-range breakout on the premium series itself.

    OR = first 30 minutes (9:15-9:45) of PREMIUM highs/lows.  Buy the first
    close above the OR high; stop at the OR midpoint; 15:15 time exit.
    One trade per day.
    """

    name = "Premium ORB"

    def __init__(self, sym: str, **kw):
        self._or_bars = kw.pop("or_bars", 30)
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", None),      # OR-mid stop instead
            target_pct=kw.pop("target_pct", None),  # ride until stop / 15:15
            max_trades_per_day=kw.pop("max_trades_per_day", 1),
            warmup_bars=kw.pop("warmup_bars", 1),
            **kw,
        )

    def _entry_signal(self, bar) -> tuple[bool, str]:
        n = len(self._closes)
        if n <= self._or_bars:
            return False, f"building opening range: {n}/{self._or_bars} bars"
        or_high = max(self._highs[:self._or_bars])
        if bar.close <= or_high:
            return False, f"premium {bar.close:.2f} below OR high {or_high:.2f}"
        return True, ""

    def _initial_stop_level(self, bar) -> float | None:
        or_high = max(self._highs[:self._or_bars])
        or_low = min(self._lows[:self._or_bars])
        return (or_high + or_low) / 2.0


# =====================================================================
# 3. Premium coil (consolidation) breakout
# =====================================================================

class PremiumCoil(_PremiumNativeStrategy):
    """Buy a volume-confirmed break out of a tight premium consolidation.

    Coil = rolling range of the previous N bars (high-low) below X% of the
    premium.  Entry when the current close breaks the coil high with
    volume >= mult x rolling average.  Stop at coil low (plus a -20% hard
    stop), +40% target, 15:15 time exit.
    """

    name = "Premium Coil Break"

    def __init__(self, sym: str, **kw):
        self._coil_bars = kw.pop("coil_bars", 15)
        self._coil_range_pct = kw.pop("coil_range_pct", 3.0) / 100.0
        self._vol_mult = kw.pop("vol_mult", 1.5)
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.20),
            target_pct=kw.pop("target_pct", 0.40),
            max_trades_per_day=kw.pop("max_trades_per_day", 3),
            warmup_bars=kw.pop("warmup_bars", 16),
            **kw,
        )
        self._coil_low: float | None = None

    def _coil_window(self) -> tuple[float, float] | None:
        """(coil_high, coil_low) over the N bars before the current one."""
        if len(self._closes) < self._coil_bars + 1:
            return None
        highs = self._highs[-self._coil_bars - 1:-1]
        lows = self._lows[-self._coil_bars - 1:-1]
        return max(highs), min(lows)

    def _entry_signal(self, bar) -> tuple[bool, str]:
        window = self._coil_window()
        if window is None:
            return False, (f"warming up coil window "
                           f"({len(self._closes)}/{self._coil_bars + 1} bars)")
        coil_high, coil_low = window
        ref = self._closes[-2]
        if ref <= 0:
            return False, "non-positive reference premium"
        if (coil_high - coil_low) / ref > self._coil_range_pct:
            return False, "no coil: range too wide"
        if bar.close <= coil_high:
            return False, "coiled but no breakout above range high"
        vols = self._volumes[-self._coil_bars - 1:-1]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "breakout without volume confirmation"
        self._coil_low = coil_low
        return True, ""

    def _initial_stop_level(self, bar) -> float | None:
        return self._coil_low


# =====================================================================
# 4. Volume surge
# =====================================================================

class VolumeSurge(_PremiumNativeStrategy):
    """Buy a 1m volume spike that coincides with a premium up-move.

    Entry: volume > k x rolling average AND close > open AND close > prev
    close.  Skips the first 15 minutes (opening volume is structurally
    elevated).  Exits: 15% trailing stop from the peak close, -25% hard
    stop, 15:15 time exit.

    Note: OI is stored in the DB but the engine's Bar has no OI field, so
    this is a volume-only surge detector.
    """

    name = "Premium Volume Surge"

    def __init__(self, sym: str, **kw):
        self._surge_mult = kw.pop("surge_mult", 3.0)
        self._vol_lookback = kw.pop("vol_lookback", 20)
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.25),
            target_pct=kw.pop("target_pct", None),
            trail_pct=kw.pop("trail_pct", 0.15),
            max_trades_per_day=kw.pop("max_trades_per_day", 3),
            warmup_bars=kw.pop("warmup_bars", 15),
            **kw,
        )

    def _entry_signal(self, bar) -> tuple[bool, str]:
        if len(self._volumes) < self._vol_lookback + 1:
            return False, (f"warming up volume window "
                           f"({len(self._volumes)}/{self._vol_lookback + 1})")
        vols = self._volumes[-self._vol_lookback - 1:-1]
        avg_vol = sum(vols) / len(vols)
        if avg_vol <= 0:
            return False, "no volume in lookback window"
        if self._volumes[-1] < self._surge_mult * avg_vol:
            return False, "no volume surge"
        if not (bar.close > bar.open and bar.close > self._closes[-2]):
            return False, "surge without premium up-move"
        return True, ""


# =====================================================================
# 5. Gamma blast (expiry day only)
# =====================================================================

class GammaBlastPremium(_PremiumNativeStrategy):
    """Expiry-day afternoon premium acceleration ("gamma blast").

    Trades ONLY when the contract expires today (expiry parsed from the
    symbol, e.g. NIFTY11AUG2624650CE -> 2026-08-11).  After 13:30, buys a
    sharp premium acceleration (+8% over 3 bars on an up bar with elevated
    volume).  -30% hard stop, 20% trail off the peak, 15:15 time exit.
    """

    name = "Gamma Blast Premium"

    def __init__(self, sym: str, **kw):
        self._accel_pct = kw.pop("accel_pct", 0.08)
        self._accel_bars = kw.pop("accel_bars", 3)
        self._vol_mult = kw.pop("vol_mult", 1.5)
        self._vol_lookback = kw.pop("vol_lookback", 20)
        self._start_time = kw.pop("start_time", time(13, 30))
        super().__init__(
            sym,
            stop_pct=kw.pop("stop_pct", 0.30),
            target_pct=kw.pop("target_pct", None),
            trail_pct=kw.pop("trail_pct", 0.20),
            max_trades_per_day=kw.pop("max_trades_per_day", 2),
            warmup_bars=kw.pop("warmup_bars", 5),
            **kw,
        )
        self._expiry = parse_option_expiry(sym)

    def _entry_signal(self, bar) -> tuple[bool, str]:
        if self._expiry is None:
            return False, f"cannot parse expiry from symbol {self._sym!r}"
        if bar.ts.date() != self._expiry:
            return False, f"not expiry day (expiry {self._expiry.isoformat()})"
        if bar.ts.time() < self._start_time:
            return False, f"before blast window {self._start_time:%H:%M}"
        n = len(self._closes)
        if n < max(self._accel_bars + 1, self._vol_lookback + 1):
            return False, "warming up acceleration/volume windows"
        base = self._closes[-self._accel_bars - 1]
        if base <= 0:
            return False, "non-positive base premium"
        accel = (bar.close - base) / base
        if accel < self._accel_pct:
            return False, (f"acceleration {accel * 100:.1f}% below "
                           f"{self._accel_pct * 100:.0f}% threshold")
        if bar.close <= bar.open:
            return False, "acceleration bar not an up bar"
        vols = self._volumes[-self._vol_lookback - 1:-1]
        avg_vol = sum(vols) / len(vols)
        if avg_vol <= 0 or self._volumes[-1] < self._vol_mult * avg_vol:
            return False, "acceleration without volume confirmation"
        return True, ""


# =====================================================================
# Registration
# =====================================================================

_COMMON_NOTE = (
    "PREMIUM-NATIVE: run on real option premium bars (symbol must end in "
    "CE/PE, e.g. NIFTY11AUG2624650CE on NFO, 1m interval). Long-only, "
    "intraday, 15:15 hard exit."
)


def _pick(kw: dict, *names: str) -> dict:
    """Whitelist known kwargs so stray service params can't break __init__."""
    return {k: kw[k] for k in names if k in kw}


_register(
    "options_premium_vwap",
    "Premium VWAP Cross",
    (
        "Buy when the option premium crosses above its intraday VWAP with "
        "rising volume. Exit on cross back below VWAP, -20% premium stop, "
        "+40% target, or 15:15. Max 3 trades/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumVwapCross(sym, **_pick(
        kw, "stop_pct", "target_pct", "max_trades_per_day",
        "warmup_bars", "capital_per_trade")),
    source="Intraday VWAP mean/trend anchor applied directly to premium.",
)

_register(
    "options_premium_orb",
    "Premium ORB",
    (
        "Opening-range breakout on the premium series: OR = 9:15-9:45 "
        "premium high/low; buy first close above OR high; stop at OR mid; "
        "time exit 15:15. 1 trade/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumORB(sym, **_pick(
        kw, "or_bars", "max_trades_per_day", "capital_per_trade")),
    source="Classic ORB, applied to premium instead of the underlying.",
)

_register(
    "options_premium_coil",
    "Premium Coil Break",
    (
        "Detect premium consolidation (15-bar range < 3% of premium) and "
        "buy the volume-confirmed break of the range high. Stop at coil "
        "low / -20%, +40% target, 15:15 exit. Max 3 trades/day. "
        + _COMMON_NOTE
    ),
    lambda sym, **kw: PremiumCoil(sym, **_pick(
        kw, "coil_bars", "coil_range_pct", "vol_mult", "stop_pct",
        "target_pct", "max_trades_per_day", "capital_per_trade")),
    source="Volatility-contraction breakout on the premium itself.",
)

_register(
    "options_volume_surge",
    "Premium Volume Surge",
    (
        "Buy when 1m volume > 3x its 20-bar average AND the premium closes "
        "up in the same bar (first 15 min excluded). 15% trailing stop, "
        "-25% hard stop, 15:15 exit. Max 3 trades/day. " + _COMMON_NOTE
    ),
    lambda sym, **kw: VolumeSurge(sym, **_pick(
        kw, "surge_mult", "vol_lookback", "stop_pct", "trail_pct",
        "max_trades_per_day", "warmup_bars", "capital_per_trade")),
    source="Order-flow surge proxy; OI unavailable on engine bars.",
)

_register(
    "options_gamma_blast_premium",
    "Gamma Blast Premium",
    (
        "Expiry-day only (expiry parsed from the symbol): after 13:30, buy "
        "a sharp premium acceleration (+8% in 3 bars, up bar, 1.5x volume). "
        "-30% hard stop, 20% trail, 15:15 exit. Max 2 trades/day. "
        + _COMMON_NOTE
    ),
    lambda sym, **kw: GammaBlastPremium(sym, **_pick(
        kw, "accel_pct", "accel_bars", "vol_mult", "vol_lookback",
        "stop_pct", "trail_pct", "max_trades_per_day", "capital_per_trade")),
    source="Expiry-afternoon gamma squeeze chase on real premium bars.",
)
