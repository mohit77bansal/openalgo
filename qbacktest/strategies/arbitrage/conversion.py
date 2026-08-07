"""Conversion / reversal arbitrage scanner.

A conversion = long stock (or future) + long put + short call (same strike, expiry).
A reversal = short stock + short put + long call.

The synthetic forward implied by options should equal the actual forward (or future
price). When it deviates, profit is locked.

Formally:
    F_implied = K + (C - P) * exp(r * t)
    Edge per unit = F_implied - F_actual

Positive edge → REVERSAL (sell synthetic, buy future).
Negative edge → CONVERSION (buy synthetic, sell future).

Difference vs PutCallParityScanner: parity expresses the edge in option-price
units; this scanner expresses it in forward-price units, useful when comparing
across multiple strikes for the same expiry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from qbacktest.core.chain import ChainSnapshot
from qbacktest.core.types import OptionType, Side
from qbacktest.strategies.arbitrage.types import (
    Opportunity,
    OpportunityKind,
    OpportunityLeg,
)


@dataclass(frozen=True)
class ConversionReversalScanner:
    """Find conversion/reversal mispricings vs the listed future."""

    min_edge_per_unit_inr: float = 3.0
    cost_per_leg_inr: float = 25.0
    margin_per_lot_estimate_inr: float = 90_000.0

    def scan(self, snapshot: ChainSnapshot) -> list[Opportunity]:
        out: list[Opportunity] = []
        if snapshot.futures_price is None:
            return out
        t = snapshot.time_to_expiry
        if t <= 0:
            return out
        for k in snapshot.strikes:
            c = snapshot.call(k)
            p = snapshot.put(k)
            if c is None or p is None:
                continue
            implied_forward = k + (c.mid - p.mid) * math.exp(snapshot.risk_free_rate * t)
            edge_per_unit = implied_forward - snapshot.futures_price
            abs_edge = abs(edge_per_unit)
            if abs_edge < self.min_edge_per_unit_inr:
                continue
            opp = self._build(snapshot, k, c, p, edge_per_unit, implied_forward)
            out.append(opp)
        return out

    def _build(
        self,
        snapshot: ChainSnapshot,
        k: float,
        c, p,
        edge_per_unit: float,
        implied_forward: float,
    ) -> Opportunity:
        lot = snapshot.lot_size
        actual_fut = snapshot.futures_price or snapshot.spot
        # Implied forward > actual: synthetic is "expensive". REVERSAL = sell synthetic, buy future.
        # synthetic short = short call + long put
        if edge_per_unit > 0:
            kind = OpportunityKind.REVERSAL
            legs = (
                OpportunityLeg(snapshot.make_instrument(k, OptionType.CALL).id, Side.SELL, lot, c.bid or c.last_price, "short_call_synth"),
                OpportunityLeg(snapshot.make_instrument(k, OptionType.PUT).id, Side.BUY, lot, p.ask or p.last_price, "long_put_synth"),
                OpportunityLeg(f"NFO:{snapshot.underlying}{snapshot.expiry:%y%b}FUT".upper(), Side.BUY, lot, actual_fut, "long_future_hedge"),
            )
            note = f"Reversal K={int(k)}: implied F={implied_forward:.2f} vs listed {actual_fut:.2f}"
        else:
            kind = OpportunityKind.CONVERSION
            legs = (
                OpportunityLeg(snapshot.make_instrument(k, OptionType.CALL).id, Side.BUY, lot, c.ask or c.last_price, "long_call_synth"),
                OpportunityLeg(snapshot.make_instrument(k, OptionType.PUT).id, Side.SELL, lot, p.bid or p.last_price, "short_put_synth"),
                OpportunityLeg(f"NFO:{snapshot.underlying}{snapshot.expiry:%y%b}FUT".upper(), Side.SELL, lot, actual_fut, "short_future_hedge"),
            )
            note = f"Conversion K={int(k)}: implied F={implied_forward:.2f} vs listed {actual_fut:.2f}"

        edge_per_lot = abs(edge_per_unit) * lot
        cost_total = 3 * self.cost_per_leg_inr
        return Opportunity(
            kind=kind,
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            legs=legs,
            theoretical_edge_inr=edge_per_lot,
            edge_per_lot_inr=edge_per_lot,
            edge_after_costs_inr=edge_per_lot - cost_total,
            max_risk_inr=cost_total + abs(edge_per_unit) * lot * 0.4,
            capital_required_inr=self.margin_per_lot_estimate_inr,
            notes=note,
            extra={"strike": k, "implied_forward": implied_forward, "actual_forward": actual_fut},
        )
