"""Box spread arbitrage scanner.

A box at strikes K1 < K2 (same expiry):
    +1 Call(K1)   -1 Call(K2)
    -1 Put(K1)    +1 Put(K2)

At expiry: box payoff = K2 - K1 (deterministic).
Today's no-arb price = (K2 - K1) * exp(-r * t).

If actual box price < theoretical, BUY the box → locked profit at expiry.
If actual box price > theoretical, SELL the box → locked profit at expiry.

We scan all (K1, K2) strike pairs and emit opportunities where the absolute
edge per unit > `min_edge_per_unit_inr` after a worst-case slippage estimate.
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
class BoxSpreadScanner:
    """Scanner config + entry point.

    Parameters
    ----------
    min_edge_per_unit_inr
        Minimum edge per UNIT (not per lot) to flag an opportunity. Default ₹2 means
        a NIFTY box (lot 75) needs ≥ ₹150 edge/lot to be worth flagging.
    max_strike_gap
        Don't bother with boxes wider than this (defaults to 1000 — 20 NIFTY strikes).
        Wider boxes have larger absolute edge but also larger margin/capital ask.
    require_two_sided_quotes
        If True, only consider strikes where both bid and ask are present (no
        one-sided stale quotes).
    """

    min_edge_per_unit_inr: float = 2.0
    max_strike_gap: float = 1000.0
    require_two_sided_quotes: bool = False
    cost_per_leg_inr: float = 25.0     # rough round-trip cost estimate per leg
    margin_per_lot_estimate_inr: float = 60_000.0   # SPAN estimate; refine via NSE margin file

    def scan(self, snapshot: ChainSnapshot) -> list[Opportunity]:
        out: list[Opportunity] = []
        strikes = snapshot.strikes
        if len(strikes) < 2:
            return out

        t = snapshot.time_to_expiry
        if t <= 0:
            return out
        df = math.exp(-snapshot.risk_free_rate * t)

        for i, k1 in enumerate(strikes):
            for k2 in strikes[i + 1 :]:
                if k2 - k1 > self.max_strike_gap:
                    break
                opp = self._evaluate_box(snapshot, k1, k2, df)
                if opp is not None:
                    out.append(opp)
        return out

    def _evaluate_box(
        self,
        snapshot: ChainSnapshot,
        k1: float,
        k2: float,
        discount_factor: float,
    ) -> Opportunity | None:
        c1 = snapshot.call(k1)
        c2 = snapshot.call(k2)
        p1 = snapshot.put(k1)
        p2 = snapshot.put(k2)
        if not all([c1, c2, p1, p2]):
            return None
        # type narrowing
        assert c1 and c2 and p1 and p2

        if self.require_two_sided_quotes:
            for q in (c1, c2, p1, p2):
                if q.bid is None or q.ask is None:
                    return None

        # Buy-box price (we pay): pay ask on long legs (C1, P2), receive bid on short legs (C2, P1)
        buy_box_cost = (
            (c1.ask if c1.ask is not None else c1.last_price)
            + (p2.ask if p2.ask is not None else p2.last_price)
            - (c2.bid if c2.bid is not None else c2.last_price)
            - (p1.bid if p1.bid is not None else p1.last_price)
        )
        # Sell-box price (we receive): bid on long legs flips to receive, ask on shorts
        sell_box_proceeds = (
            (c1.bid if c1.bid is not None else c1.last_price)
            + (p2.bid if p2.bid is not None else p2.last_price)
            - (c2.ask if c2.ask is not None else c2.last_price)
            - (p1.ask if p1.ask is not None else p1.last_price)
        )

        theoretical_box_price = (k2 - k1) * discount_factor

        # Long-box opportunity: buy box for less than theoretical
        long_box_edge_per_unit = theoretical_box_price - buy_box_cost
        # Short-box opportunity: sell box for more than theoretical
        short_box_edge_per_unit = sell_box_proceeds - theoretical_box_price

        # Pick the better side
        if long_box_edge_per_unit >= short_box_edge_per_unit and long_box_edge_per_unit >= self.min_edge_per_unit_inr:
            return self._build_opp(
                snapshot, k1, k2, c1, c2, p1, p2,
                long=True, edge_per_unit=long_box_edge_per_unit,
                box_price=buy_box_cost, theoretical_box_price=theoretical_box_price,
            )
        if short_box_edge_per_unit >= self.min_edge_per_unit_inr:
            return self._build_opp(
                snapshot, k1, k2, c1, c2, p1, p2,
                long=False, edge_per_unit=short_box_edge_per_unit,
                box_price=sell_box_proceeds, theoretical_box_price=theoretical_box_price,
            )
        return None

    def _build_opp(
        self,
        snapshot: ChainSnapshot,
        k1: float, k2: float,
        c1, c2, p1, p2,
        *,
        long: bool,
        edge_per_unit: float,
        box_price: float,
        theoretical_box_price: float,
    ) -> Opportunity:
        lot = snapshot.lot_size
        # Long box construction: +C(K1) -C(K2) -P(K1) +P(K2)
        if long:
            legs = (
                OpportunityLeg(snapshot.make_instrument(k1, OptionType.CALL).id, Side.BUY, lot, c1.ask or c1.last_price, "long_call_low"),
                OpportunityLeg(snapshot.make_instrument(k2, OptionType.CALL).id, Side.SELL, lot, c2.bid or c2.last_price, "short_call_high"),
                OpportunityLeg(snapshot.make_instrument(k1, OptionType.PUT).id, Side.SELL, lot, p1.bid or p1.last_price, "short_put_low"),
                OpportunityLeg(snapshot.make_instrument(k2, OptionType.PUT).id, Side.BUY, lot, p2.ask or p2.last_price, "long_put_high"),
            )
        else:
            legs = (
                OpportunityLeg(snapshot.make_instrument(k1, OptionType.CALL).id, Side.SELL, lot, c1.bid or c1.last_price, "short_call_low"),
                OpportunityLeg(snapshot.make_instrument(k2, OptionType.CALL).id, Side.BUY, lot, c2.ask or c2.last_price, "long_call_high"),
                OpportunityLeg(snapshot.make_instrument(k1, OptionType.PUT).id, Side.BUY, lot, p1.ask or p1.last_price, "long_put_low"),
                OpportunityLeg(snapshot.make_instrument(k2, OptionType.PUT).id, Side.SELL, lot, p2.bid or p2.last_price, "short_put_high"),
            )

        edge_per_lot = edge_per_unit * lot
        # 4 legs, each costs ~₹25 round trip (this is a held-to-expiry trade so one-way of cost).
        # Conservative: 4 × 25 = 100.
        cost_total = 4 * self.cost_per_leg_inr
        edge_after_costs = edge_per_lot - cost_total
        return Opportunity(
            kind=OpportunityKind.BOX_SPREAD,
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            legs=legs,
            theoretical_edge_inr=edge_per_lot,
            edge_per_lot_inr=edge_per_lot,
            edge_after_costs_inr=edge_after_costs,
            max_risk_inr=cost_total + abs(edge_per_unit) * lot * 0.3,  # slippage worst case
            capital_required_inr=self.margin_per_lot_estimate_inr,
            notes=(
                f"Box {int(k1)}/{int(k2)} {('LONG' if long else 'SHORT')} | "
                f"actual={box_price:.2f} theoretical={theoretical_box_price:.2f} | "
                f"edge/unit ₹{edge_per_unit:.2f}"
            ),
            extra={"k1": k1, "k2": k2, "long": long, "actual_box_price": box_price, "theoretical_box_price": theoretical_box_price},
        )
