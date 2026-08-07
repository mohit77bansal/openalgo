"""Put-call parity scanner.

For European options on the same underlying + strike + expiry:
    C - P = S * e^(-q*t) - K * e^(-r*t)

If actual (C - P) deviates from theoretical, an arbitrage is available.
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
class PutCallParityScanner:
    """Scan for put-call parity violations.

    Edge per unit = |actual(C - P) - theoretical(C - P)|.

    The parity hedge requires a futures position (or a synthetic-stock equivalent).
    For NIFTY/BANKNIFTY index, futures are the natural hedge.
    """

    min_edge_per_unit_inr: float = 3.0
    cost_per_leg_inr: float = 25.0
    margin_per_lot_estimate_inr: float = 90_000.0  # 2 options + 1 future

    def scan(self, snapshot: ChainSnapshot) -> list[Opportunity]:
        out: list[Opportunity] = []
        t = snapshot.time_to_expiry
        if t <= 0:
            return out
        # Use futures price if available, else spot
        forward = snapshot.futures_price if snapshot.futures_price is not None else snapshot.spot
        df = math.exp(-snapshot.risk_free_rate * t)

        for k in snapshot.strikes:
            c = snapshot.call(k)
            p = snapshot.put(k)
            if c is None or p is None:
                continue
            # Theoretical C - P (using forward, assuming q ≈ 0 for index)
            theoretical_diff = (forward - k) * df
            actual_diff = c.mid - p.mid
            edge_per_unit = actual_diff - theoretical_diff

            abs_edge = abs(edge_per_unit)
            if abs_edge < self.min_edge_per_unit_inr:
                continue

            opp = self._build_opp(snapshot, k, c, p, edge_per_unit, theoretical_diff, actual_diff)
            out.append(opp)
        return out

    def _build_opp(
        self,
        snapshot: ChainSnapshot,
        k: float,
        c, p,
        edge_per_unit: float,
        theoretical_diff: float,
        actual_diff: float,
    ) -> Opportunity:
        lot = snapshot.lot_size
        # If actual > theoretical: call is rich. SELL call, BUY put, BUY future.
        # If actual < theoretical: put is rich. BUY call, SELL put, SELL future.
        if edge_per_unit > 0:
            kind = OpportunityKind.PCP_CALL_RICH
            legs = (
                OpportunityLeg(snapshot.make_instrument(k, OptionType.CALL).id, Side.SELL, lot, c.bid or c.last_price, "short_call"),
                OpportunityLeg(snapshot.make_instrument(k, OptionType.PUT).id, Side.BUY, lot, p.ask or p.last_price, "long_put"),
                # Future leg — represented symbolically; live wiring needs actual instrument lookup.
                OpportunityLeg(f"NFO:{snapshot.underlying}{snapshot.expiry:%y%b}FUT".upper(), Side.BUY, lot, snapshot.futures_price or snapshot.spot, "long_future"),
            )
            note = f"Call rich at K={int(k)}: C-P={actual_diff:.2f} vs theoretical {theoretical_diff:.2f}"
        else:
            kind = OpportunityKind.PCP_PUT_RICH
            legs = (
                OpportunityLeg(snapshot.make_instrument(k, OptionType.CALL).id, Side.BUY, lot, c.ask or c.last_price, "long_call"),
                OpportunityLeg(snapshot.make_instrument(k, OptionType.PUT).id, Side.SELL, lot, p.bid or p.last_price, "short_put"),
                OpportunityLeg(f"NFO:{snapshot.underlying}{snapshot.expiry:%y%b}FUT".upper(), Side.SELL, lot, snapshot.futures_price or snapshot.spot, "short_future"),
            )
            note = f"Put rich at K={int(k)}: C-P={actual_diff:.2f} vs theoretical {theoretical_diff:.2f}"

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
            extra={"strike": k, "actual_diff": actual_diff, "theoretical_diff": theoretical_diff},
        )
