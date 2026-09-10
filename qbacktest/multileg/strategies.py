"""Multi-leg option structures as backtestable strategies.

Each strategy enters at most one structure per day and hands it to
``simulate_structure`` for close-to-close management. They are deliberately
diverse in *what bet they make on volatility and direction*:

  credit / theta-collecting (win when the market sits still):
    - short_straddle       sell ATM CE + ATM PE          (undefined risk)
    - iron_fly             sell ATM CE+PE, buy wings      (defined risk)
    - iron_condor          sell OTM strangle, buy wings   (defined risk)

  debit / directional (defined risk, cheap — fit a small book):
    - bull_call_spread     buy ATM CE, sell OTM CE        (bullish)
    - bear_put_spread      buy ATM PE, sell OTM PE        (bearish)

  long volatility (win on a big move either way):
    - long_straddle        buy ATM CE + ATM PE
    - long_strangle        buy OTM CE + OTM PE            (cheaper)

Strike selection always snaps to the nearest *listed* strike, so a structure
never references a leg the chain doesn't have.
"""

from __future__ import annotations

from typing import Optional

from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side
from qbacktest.costs import CostModel
from qbacktest.multileg.backtest import (
    Leg,
    NIFTY_LOT,
    Session,
    TradeResult,
    bar_at_or_after,
    bar_at_or_before,
    simulate_structure,
)

ENTRY_H, ENTRY_M = 9, 25          # let the open settle a few minutes
EXIT_H, EXIT_M = 15, 10           # square off before the 15:30 expiry/close


def _pick(session: Session, i: int, target_strike: int, opt_type: str) -> Optional[int]:
    """Nearest listed strike to ``target_strike`` that has a quote at bar i."""
    book = session.ce if opt_type == "CE" else session.pe
    have = [k for k in book if session.price(i, k, opt_type) not in (None, 0)]
    if not have:
        return None
    return min(have, key=lambda k: abs(k - target_strike))


def _window(session: Session) -> Optional[tuple[int, int]]:
    ei = bar_at_or_after(session, ENTRY_H, ENTRY_M)
    xi = bar_at_or_before(session, EXIT_H, EXIT_M)
    if ei is None or xi is None or xi <= ei:
        return None
    return ei, xi


# Rough margin/capital blocked per structure (NIFTY, 1 lot). Credit structures
# with undefined risk use SPAN-ish ballparks; defined-risk ones use max loss.
def _defined_risk_margin(legs: tuple[Leg, ...], session: Session, i: int) -> float:
    """Max loss of a defined-risk vertical/condor = width*lot - net credit
    (credit) or net debit (debit spread). Computed from entry prices."""
    lot = NIFTY_LOT
    net = 0.0
    prices = {}
    for leg in legs:
        p = session.price(i, leg.strike, leg.opt_type)
        if p is None:
            return 0.0
        prices[leg] = p
        net += (p * lot) if leg.side == "BUY" else -(p * lot)
    # widest same-type spread width
    ce_k = sorted(k.strike for k in legs if k.opt_type == "CE")
    pe_k = sorted(k.strike for k in legs if k.opt_type == "PE")
    width = 0
    if len(ce_k) >= 2:
        width = max(width, ce_k[-1] - ce_k[0])
    if len(pe_k) >= 2:
        width = max(width, pe_k[-1] - pe_k[0])
    if net > 0:            # debit structure — max loss is the debit
        return round(net, 0)
    return round(width * lot + net, 0)   # credit structure — width*lot - credit


# ---------------------------------------------------------------------------
# Credit / theta structures
# ---------------------------------------------------------------------------


def short_straddle(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce = _pick(session, ei, atm, "CE")
    pe = _pick(session, ei, atm, "PE")
    if ce is None or pe is None:
        return []
    legs = (Leg(ce, "CE", "SELL"), Leg(pe, "PE", "SELL"))
    r = simulate_structure(
        session, ei, legs, label="short_straddle",
        entry_reason=f"sell ATM {atm} straddle @09:25",
        target_frac=0.30, stop_frac=0.60, exit_i_max=xi,
        cost_model=cost_model, margin_est=150_000,
    )
    return [r] if r else []


def iron_fly(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce = _pick(session, ei, atm, "CE")
    pe = _pick(session, ei, atm, "PE")
    ce_w = _pick(session, ei, atm + 200, "CE")
    pe_w = _pick(session, ei, atm - 200, "PE")
    if None in (ce, pe, ce_w, pe_w) or ce_w == ce or pe_w == pe:
        return []
    legs = (
        Leg(ce, "CE", "SELL"), Leg(pe, "PE", "SELL"),
        Leg(ce_w, "CE", "BUY"), Leg(pe_w, "PE", "BUY"),
    )
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="iron_fly",
        entry_reason=f"sell ATM {atm} fly, wings +/-200 @09:25",
        target_frac=0.25, stop_frac=0.60, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


def iron_condor(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce_s = _pick(session, ei, atm + 100, "CE")   # sell OTM strangle
    pe_s = _pick(session, ei, atm - 100, "PE")
    ce_w = _pick(session, ei, atm + 300, "CE")   # buy wings
    pe_w = _pick(session, ei, atm - 300, "PE")
    if None in (ce_s, pe_s, ce_w, pe_w) or ce_w == ce_s or pe_w == pe_s:
        return []
    legs = (
        Leg(ce_s, "CE", "SELL"), Leg(pe_s, "PE", "SELL"),
        Leg(ce_w, "CE", "BUY"), Leg(pe_w, "PE", "BUY"),
    )
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="iron_condor",
        entry_reason=f"sell {atm}+/-100 strangle, wings +/-300 @09:25",
        target_frac=0.25, stop_frac=0.60, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


# ---------------------------------------------------------------------------
# Debit / directional structures (cheap, defined risk)
# ---------------------------------------------------------------------------


def bull_call_spread(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    buy = _pick(session, ei, atm, "CE")
    sell = _pick(session, ei, atm + 200, "CE")
    if None in (buy, sell) or buy == sell:
        return []
    legs = (Leg(buy, "CE", "BUY"), Leg(sell, "CE", "SELL"))
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="bull_call_spread",
        entry_reason=f"buy {buy}CE / sell {sell}CE @09:25",
        target_frac=0.60, stop_frac=0.50, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


def bear_put_spread(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    buy = _pick(session, ei, atm, "PE")
    sell = _pick(session, ei, atm - 200, "PE")
    if None in (buy, sell) or buy == sell:
        return []
    legs = (Leg(buy, "PE", "BUY"), Leg(sell, "PE", "SELL"))
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="bear_put_spread",
        entry_reason=f"buy {buy}PE / sell {sell}PE @09:25",
        target_frac=0.60, stop_frac=0.50, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


# ---------------------------------------------------------------------------
# Long volatility
# ---------------------------------------------------------------------------


def long_straddle(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce = _pick(session, ei, atm, "CE")
    pe = _pick(session, ei, atm, "PE")
    if None in (ce, pe):
        return []
    legs = (Leg(ce, "CE", "BUY"), Leg(pe, "PE", "BUY"))
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="long_straddle",
        entry_reason=f"buy ATM {atm} straddle @09:25",
        target_frac=0.40, stop_frac=0.35, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


def long_strangle(session: Session, cost_model: CostModel) -> list[TradeResult]:
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce = _pick(session, ei, atm + 100, "CE")
    pe = _pick(session, ei, atm - 100, "PE")
    if None in (ce, pe):
        return []
    legs = (Leg(ce, "CE", "BUY"), Leg(pe, "PE", "BUY"))
    margin = _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="long_strangle",
        entry_reason=f"buy {ce}CE / {pe}PE strangle @09:25",
        target_frac=0.50, stop_frac=0.40, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


def adaptive_straddle(session: Session, cost_model: CostModel) -> list[TradeResult]:
    """Regime-gated ATM straddle: BUY vol when the open already trends, SELL
    vol when the open is quiet.

    The backtest showed a clean split — premium *sellers* win on range days and
    *buyers* win on trend days. This gates on the opening move: if spot has
    already travelled >= OPEN_MOVE_PTS points from the first bar by entry time,
    a directional day is likely (buy the straddle); otherwise expect
    mean-reversion/range (sell it). One trade/day. Sample is tiny — treat as a
    mechanism demo, not a proven edge.
    """
    OPEN_MOVE_PTS = 40.0
    w = _window(session)
    if not w:
        return []
    ei, xi = w
    atm = session.atm_strike(ei)
    ce = _pick(session, ei, atm, "CE")
    pe = _pick(session, ei, atm, "PE")
    if None in (ce, pe):
        return []
    opening_move = abs(session.spot[ei] - session.spot[0])
    if opening_move >= OPEN_MOVE_PTS:
        side, tf, sf, reason = "BUY", 0.40, 0.35, f"open moved {opening_move:.0f}pts -> buy vol"
    else:
        side, tf, sf, reason = "SELL", 0.30, 0.60, f"open quiet {opening_move:.0f}pts -> sell vol"
    legs = (Leg(ce, "CE", side), Leg(pe, "PE", side))
    margin = 150_000 if side == "SELL" else _defined_risk_margin(legs, session, ei)
    r = simulate_structure(
        session, ei, legs, label="adaptive_straddle",
        entry_reason=reason, target_frac=tf, stop_frac=sf, exit_i_max=xi,
        cost_model=cost_model, margin_est=margin,
    )
    return [r] if r else []


ALL_STRATEGIES = {
    "short_straddle": short_straddle,
    "iron_fly": iron_fly,
    "iron_condor": iron_condor,
    "bull_call_spread": bull_call_spread,
    "bear_put_spread": bear_put_spread,
    "long_straddle": long_straddle,
    "long_strangle": long_strangle,
    "adaptive_straddle": adaptive_straddle,
}
