"""Multi-leg option-structure backtester.

The single-leg engine streams one contract at a time, so it cannot price a
straddle / spread / iron-condor whose P&L depends on *several* legs at once.
This module fixes that: it loads the near-ATM chain for one expiry into a
timestamp-aligned matrix (every leg has a price at every bar) and simulates
whole *structures* — a set of BUY/SELL legs entered and exited together.

Design choices (kept deliberately honest):

* **Close-to-close fills.** Entry and exit use the 1m *close*. Intrabar
  target/stop peeking would flatter the numbers; we don't do it. Slippage is
  therefore modelled only via the cost model's spread assumptions, and the
  results read as "managed on close", which is conservative for targets and
  slightly optimistic for stops. Flagged, not hidden.
* **One structure per day.** Real multi-leg income/directional trades are
  entered once and managed; we don't pyramid. This matches how a retail
  account would actually run these on a 1-lakh book.
* **Costs are per-leg, per-fill.** Every leg pays entry + exit costs through
  the same Indian cost model the single-leg engine uses (STT on the sell-side
  premium, GST, exchange txn, SEBI, stamp duty, ₹20 brokerage cap).

Nothing here mutates its inputs; every result is a fresh dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import duckdb

from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_LOT = 75


# ---------------------------------------------------------------------------
# Domain types (all frozen — mutate via dataclasses.replace)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Leg:
    """One leg of a structure. ``lots`` is contract lots; qty = lots * lot_size."""

    strike: int
    opt_type: str  # 'CE' | 'PE'
    side: str      # 'BUY' | 'SELL'
    lots: int = 1


@dataclass(frozen=True, slots=True)
class Session:
    """One trading day's aligned chain for a single expiry.

    ``ts`` is the sorted list of epoch-second bar timestamps common to the
    spot series. ``spot[i]`` is the future close at bar ``i``. ``ce`` / ``pe``
    map strike -> list-of-close aligned to ``ts`` (None where that leg has no
    bar at that timestamp). ``oi_*`` mirror that for open interest.
    """

    day: date
    ts: list[int]
    spot: list[float]
    ce: dict[int, list[Optional[float]]]
    pe: dict[int, list[Optional[float]]]
    oi_ce: dict[int, list[Optional[float]]] = field(default_factory=dict)
    oi_pe: dict[int, list[Optional[float]]] = field(default_factory=dict)

    def price(self, i: int, strike: int, opt_type: str) -> Optional[float]:
        book = self.ce if opt_type == "CE" else self.pe
        col = book.get(strike)
        if col is None:
            return None
        return col[i]

    def minute_ist(self, i: int) -> tuple[int, int]:
        dt = datetime.fromtimestamp(self.ts[i], tz=IST)
        return dt.hour, dt.minute

    def strikes(self, opt_type: str) -> list[int]:
        return sorted((self.ce if opt_type == "CE" else self.pe).keys())

    def atm_strike(self, i: int, step: int = 50) -> int:
        """Nearest listed strike to spot at bar ``i`` that exists on BOTH sides."""
        s = self.spot[i]
        both = sorted(set(self.ce.keys()) & set(self.pe.keys()))
        if not both:
            return int(round(s / step) * step)
        return min(both, key=lambda k: abs(k - s))


@dataclass(frozen=True, slots=True)
class TradeResult:
    day: date
    label: str
    entry_i: int
    exit_i: int
    entry_reason: str
    exit_reason: str
    legs: tuple[Leg, ...]
    net_premium: float      # +debit paid / -credit received, per structure (₹)
    gross_pnl: float        # MTM at exit, before costs (₹)
    costs: float            # round-trip costs across all legs (₹)
    net_pnl: float          # gross - costs (₹)
    margin_est: float       # rough margin/capital blocked (₹)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _epoch_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=IST).date()


def load_sessions(
    db_path: str,
    expiry_tag: str,
    *,
    spot_symbol: str,
    strike_lo: int,
    strike_hi: int,
    min_bars: int = 1500,
) -> list[Session]:
    """Build per-day aligned Sessions for one expiry's near-ATM chain.

    ``expiry_tag`` is the symbol-embedded expiry, e.g. ``11AUG26`` so the
    contract symbols look like ``NIFTY11AUG26<strike><CE|PE>``.
    """
    conn = duckdb.connect(db_path, read_only=True)
    try:
        # Spot / future series (the structure's underlying reference).
        spot_rows = conn.execute(
            """
            SELECT timestamp, close FROM market_data
            WHERE symbol = ? AND interval = '1m'
            ORDER BY timestamp
            """,
            [spot_symbol],
        ).fetchall()

        # Every near-ATM leg for this expiry with enough coverage.
        leg_rows = conn.execute(
            """
            SELECT symbol, timestamp, close, oi FROM market_data
            WHERE symbol LIKE ? AND interval = '1m'
            ORDER BY timestamp
            """,
            [f"NIFTY{expiry_tag}%"],
        ).fetchall()
    finally:
        conn.close()

    import re

    pat = re.compile(rf"^NIFTY{expiry_tag}(\d+)(CE|PE)$")

    # spot[ts] lookup
    spot_by_ts: dict[int, float] = {ts: c for ts, c in spot_rows}

    # leg close/oi keyed by (strike, type) -> {ts: value}
    close_by: dict[tuple[int, str], dict[int, float]] = {}
    oi_by: dict[tuple[int, str], dict[int, float]] = {}
    coverage: dict[tuple[int, str], int] = {}
    for sym, ts, close, oi in leg_rows:
        m = pat.match(sym)
        if not m:
            continue
        strike = int(m.group(1))
        if not (strike_lo <= strike <= strike_hi):
            continue
        key = (strike, m.group(2))
        close_by.setdefault(key, {})[ts] = close
        oi_by.setdefault(key, {})[ts] = oi
        coverage[key] = coverage.get(key, 0) + 1

    # Keep only well-covered legs.
    good = {k for k, n in coverage.items() if n >= min_bars}

    # Group timestamps by day, restricted to bars where spot exists.
    days: dict[date, list[int]] = {}
    for ts in sorted(spot_by_ts):
        days.setdefault(_epoch_day(ts), []).append(ts)

    sessions: list[Session] = []
    for day, ts_list in sorted(days.items()):
        # A leg is included for the day only if it trades that day at all.
        ce: dict[int, list[Optional[float]]] = {}
        pe: dict[int, list[Optional[float]]] = {}
        oi_ce: dict[int, list[Optional[float]]] = {}
        oi_pe: dict[int, list[Optional[float]]] = {}
        for (strike, typ) in good:
            cmap = close_by[(strike, typ)]
            if not any(ts in cmap for ts in ts_list):
                continue
            omap = oi_by[(strike, typ)]
            col = [cmap.get(ts) for ts in ts_list]
            ocol = [omap.get(ts) for ts in ts_list]
            if typ == "CE":
                ce[strike] = col
                oi_ce[strike] = ocol
            else:
                pe[strike] = col
                oi_pe[strike] = ocol
        if not ce or not pe:
            continue
        spot = [spot_by_ts[ts] for ts in ts_list]
        sessions.append(
            Session(day=day, ts=ts_list, spot=spot, ce=ce, pe=pe, oi_ce=oi_ce, oi_pe=oi_pe)
        )
    return sessions


# ---------------------------------------------------------------------------
# Structure simulation
# ---------------------------------------------------------------------------


def _leg_cost(model: CostModel, leg: Leg, side: Side, price: float, trade_date: date) -> float:
    itype = InstrumentType.CE if leg.opt_type == "CE" else InstrumentType.PE
    cb = model.compute_fill(
        exchange=Exchange.NFO,
        instrument_type=itype,
        product=ProductType.NRML,
        side=side,
        quantity=leg.lots * NIFTY_LOT,
        price=price,
        trade_date=trade_date,
    )
    return cb.total


def _priced_at(session: Session, i: int, legs: tuple[Leg, ...]) -> Optional[dict[Leg, float]]:
    """All leg prices at bar i, or None if any leg is missing a quote."""
    out: dict[Leg, float] = {}
    for leg in legs:
        p = session.price(i, leg.strike, leg.opt_type)
        if p is None or p <= 0:
            return None
        out[leg] = p
    return out


def simulate_structure(
    session: Session,
    entry_i: int,
    legs: tuple[Leg, ...],
    *,
    label: str,
    entry_reason: str,
    target_frac: float,
    stop_frac: float,
    exit_i_max: int,
    cost_model: CostModel,
    margin_est: float,
) -> Optional[TradeResult]:
    """Enter ``legs`` at ``entry_i``, manage on close until target/stop/time.

    ``target_frac`` / ``stop_frac`` are fractions of the absolute entry net
    premium (credit for sold structures, debit for bought ones). A sold
    structure profits as MTM rises toward +target; a bought one the same. The
    reference is symmetric so both credit and debit structures use one code
    path.
    """
    entry_prices = _priced_at(session, entry_i, legs)
    if entry_prices is None:
        return None

    lot = NIFTY_LOT
    # net premium: +ve = debit paid, -ve = credit received.
    net_premium = 0.0
    for leg, px in entry_prices.items():
        qty = leg.lots * lot
        net_premium += (px * qty) if leg.side == "BUY" else -(px * qty)

    ref = abs(net_premium)
    if ref <= 0:
        return None
    target_amt = target_frac * ref
    stop_amt = stop_frac * ref

    def mtm(prices: dict[Leg, float]) -> float:
        total = 0.0
        for leg, px in prices.items():
            qty = leg.lots * lot
            entry_px = entry_prices[leg]
            if leg.side == "BUY":
                total += (px - entry_px) * qty
            else:
                total += (entry_px - px) * qty
        return total

    exit_i = exit_i_max
    exit_reason = "time_exit"
    exit_prices = None
    for j in range(entry_i + 1, exit_i_max + 1):
        pj = _priced_at(session, j, legs)
        if pj is None:
            continue
        m = mtm(pj)
        if m >= target_amt:
            exit_i, exit_reason, exit_prices = j, "target", pj
            break
        if m <= -stop_amt:
            exit_i, exit_reason, exit_prices = j, "stop", pj
            break
        exit_i, exit_prices = j, pj  # trail the last valid quote for time-exit

    if exit_prices is None:
        return None

    gross = mtm(exit_prices)

    # Round-trip costs: entry fill (leg.side) + exit fill (opposite side).
    costs = 0.0
    for leg, epx in entry_prices.items():
        entry_side = Side.BUY if leg.side == "BUY" else Side.SELL
        exit_side = Side.SELL if leg.side == "BUY" else Side.BUY
        costs += _leg_cost(cost_model, leg, entry_side, epx, session.day)
        costs += _leg_cost(cost_model, leg, exit_side, exit_prices[leg], session.day)

    return TradeResult(
        day=session.day,
        label=label,
        entry_i=entry_i,
        exit_i=exit_i,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        legs=legs,
        net_premium=round(net_premium, 2),
        gross_pnl=round(gross, 2),
        costs=round(costs, 2),
        net_pnl=round(gross - costs, 2),
        margin_est=margin_est,
    )


def bar_at_or_after(session: Session, hour: int, minute: int) -> Optional[int]:
    """First bar index at/after the given IST wall-clock time."""
    for i in range(len(session.ts)):
        h, m = session.minute_ist(i)
        if (h, m) >= (hour, minute):
            return i
    return None


def bar_at_or_before(session: Session, hour: int, minute: int) -> Optional[int]:
    """Last bar index at/before the given IST wall-clock time."""
    last = None
    for i in range(len(session.ts)):
        h, m = session.minute_ist(i)
        if (h, m) <= (hour, minute):
            last = i
        else:
            break
    return last


# A Strategy takes a Session + cost model and returns 0..1 TradeResult(s).
Strategy = Callable[[Session, CostModel], list[TradeResult]]


def run_strategy(sessions: list[Session], strategy: Strategy, cost_model: CostModel) -> list[TradeResult]:
    out: list[TradeResult] = []
    for s in sessions:
        try:
            out.extend(strategy(s, cost_model))
        except Exception:
            # A single bad day must not sink the whole backtest; skip it.
            continue
    return out
