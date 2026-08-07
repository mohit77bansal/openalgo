"""Far-OTM premium-collection scanner — flagged with mandatory hedge requirement.

This is NOT pure arbitrage. It's a quasi-arb / lottery sale that has positive
expected value most days and catastrophic loss on tail events.

We REFUSE to surface naked-sell opportunities. Every flagged opportunity must be
paired with a tail-hedge leg (buying a deeper-OTM option to cap maximum loss).

This makes it a pre-built credit spread, not a naked sale. Retail blow-up
prevention is a feature.

See `docs/RESEARCH.md §6` for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from qbacktest.core.chain import ChainSnapshot
from qbacktest.core.types import OptionType, Side
from qbacktest.greeks import bs_price
from qbacktest.strategies.arbitrage.types import (
    Opportunity,
    OpportunityKind,
    OpportunityLeg,
)


@dataclass(frozen=True)
class FarOTMPremiumScanner:
    """Identify rich far-OTM premium with a paired hedge leg.

    Parameters
    ----------
    distance_from_atm_pct
        Strike distance from spot, e.g. 0.05 = 5%.
    min_premium_inr
        Skip strikes priced below this (no juice).
    iv_threshold_for_richness
        If implied vol > this multiple of mean ATM IV, the strike is "rich".
    """

    distance_from_atm_pct: float = 0.05
    min_premium_inr: float = 1.0
    iv_threshold_for_richness: float = 1.30
    cost_per_leg_inr: float = 25.0
    margin_per_lot_estimate_inr: float = 50_000.0

    def scan(self, snapshot: ChainSnapshot, *, atm_iv_estimate: Optional[float] = None) -> list[Opportunity]:
        out: list[Opportunity] = []
        t = snapshot.time_to_expiry
        if t <= 0:
            return out

        # ATM IV estimate from chain if not provided
        if atm_iv_estimate is None:
            atm_strike = snapshot.atm_strike()
            atm_call = snapshot.call(atm_strike)
            if atm_call and atm_call.iv:
                atm_iv_estimate = atm_call.iv
            else:
                atm_iv_estimate = 0.18  # default fallback

        # Look for OTM puts (downside) and OTM calls (upside) priced "rich"
        spot = snapshot.spot
        for q in snapshot.quotes:
            distance_pct = abs(q.strike - spot) / spot
            if distance_pct < self.distance_from_atm_pct:
                continue
            if q.last_price < self.min_premium_inr:
                continue
            # Get the IV — use stored if present, else compute
            iv = q.iv
            if iv is None:
                # Skip — we don't recompute here to avoid heavy work; assumes upstream populated
                continue
            if iv < self.iv_threshold_for_richness * atm_iv_estimate:
                continue

            # Find a hedge leg: deeper-OTM strike, same type
            hedge = self._find_hedge(snapshot, q)
            if hedge is None:
                continue

            opp = self._build(snapshot, q, hedge)
            if opp is not None:
                out.append(opp)
        return out

    def _find_hedge(self, snapshot: ChainSnapshot, sell_quote) -> Optional:
        """Pick a deeper-OTM strike of the same type as the hedge leg."""
        same_type = [q for q in snapshot.quotes if q.option_type == sell_quote.option_type]
        if sell_quote.option_type == OptionType.CALL:
            # Hedge = higher strike call
            candidates = [q for q in same_type if q.strike > sell_quote.strike]
        else:
            # Hedge = lower strike put
            candidates = [q for q in same_type if q.strike < sell_quote.strike]
        if not candidates:
            return None
        # Pick the closest one with a real price
        candidates.sort(key=lambda q: abs(q.strike - sell_quote.strike))
        for c in candidates:
            if c.last_price >= 0.05:
                return c
        return None

    def _build(self, snapshot: ChainSnapshot, sell, hedge) -> Optional[Opportunity]:
        lot = snapshot.lot_size
        # Net credit = premium received - hedge cost
        sell_px = sell.bid if sell.bid is not None else sell.last_price
        hedge_px = hedge.ask if hedge.ask is not None else hedge.last_price
        credit_per_unit = sell_px - hedge_px
        if credit_per_unit < 0.5:
            return None

        legs = (
            OpportunityLeg(snapshot.make_instrument(sell.strike, sell.option_type).id, Side.SELL, lot, sell_px, "short_rich"),
            OpportunityLeg(snapshot.make_instrument(hedge.strike, hedge.option_type).id, Side.BUY, lot, hedge_px, "long_hedge"),
        )
        max_loss_per_lot = abs(sell.strike - hedge.strike) * lot - credit_per_unit * lot
        edge_per_lot = credit_per_unit * lot
        return Opportunity(
            kind=OpportunityKind.FAR_OTM_PREMIUM,
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            legs=legs,
            theoretical_edge_inr=edge_per_lot,
            edge_per_lot_inr=edge_per_lot,
            edge_after_costs_inr=edge_per_lot - 2 * self.cost_per_leg_inr,
            max_risk_inr=max_loss_per_lot,
            capital_required_inr=self.margin_per_lot_estimate_inr,
            notes=(
                f"Far-OTM credit spread {int(sell.strike)}/{int(hedge.strike)} "
                f"({sell.option_type.value}): credit ₹{credit_per_unit:.2f}/unit, "
                f"max loss ₹{max_loss_per_lot:.0f}/lot. Quasi-arb with REAL TAIL RISK."
            ),
            extra={
                "sell_strike": sell.strike,
                "hedge_strike": hedge.strike,
                "sell_iv": sell.iv,
                "max_loss_per_lot": max_loss_per_lot,
                "warning": "REAL_LOSS_POSSIBLE",
            },
        )
