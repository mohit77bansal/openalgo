"""Adjacent-strike vertical inversion scanner.

For calls with strikes K1 < K2 (same expiry):
    C(K1) >= C(K2) by no-arbitrage.

If observed prices violate this (C(K1) < C(K2)), then buying C(K1) and selling
C(K2) yields a CREDIT and a non-negative payoff at expiry — free money.

Symmetric for puts: P(K2) >= P(K1) when K1 < K2.

These appear in real Indian markets at far-OTM strikes where market makers don't
quote tight. Disappear within seconds — alert needs to be sub-second.
"""

from __future__ import annotations

from dataclasses import dataclass

from qbacktest.core.chain import ChainSnapshot
from qbacktest.core.types import OptionType, Side
from qbacktest.strategies.arbitrage.types import (
    Opportunity,
    OpportunityKind,
    OpportunityLeg,
)


@dataclass(frozen=True)
class AdjacentStrikeInversionScanner:
    min_credit_per_unit_inr: float = 0.5
    cost_per_leg_inr: float = 25.0
    margin_per_lot_estimate_inr: float = 30_000.0   # vertical spread max-loss + buffer

    def scan(self, snapshot: ChainSnapshot) -> list[Opportunity]:
        out: list[Opportunity] = []
        # Calls: walk strikes ascending; for each adjacent pair check inversion
        calls = snapshot.calls()
        puts = snapshot.puts()

        # Use bid/ask to be conservative: we BUY the cheap leg (pay ask), SELL the
        # expensive leg (receive bid). Inversion exists iff bid_higher_strike > ask_lower_strike
        # which is a *real* tradable mispricing.
        for i in range(len(calls) - 1):
            low = calls[i]
            high = calls[i + 1]
            buy_low_ask = low.ask if low.ask is not None else low.last_price
            sell_high_bid = high.bid if high.bid is not None else high.last_price
            credit_per_unit = sell_high_bid - buy_low_ask
            if credit_per_unit > self.min_credit_per_unit_inr and high.strike > low.strike:
                out.append(self._build_call(snapshot, low, high, credit_per_unit, buy_low_ask, sell_high_bid))

        for i in range(len(puts) - 1):
            low = puts[i]
            high = puts[i + 1]
            # For puts: P(K_high) >= P(K_low). Inversion: P(K_low) > P(K_high).
            # Trade: buy P(K_high) cheap, sell P(K_low) expensive.
            buy_high_ask = high.ask if high.ask is not None else high.last_price
            sell_low_bid = low.bid if low.bid is not None else low.last_price
            credit_per_unit = sell_low_bid - buy_high_ask
            if credit_per_unit > self.min_credit_per_unit_inr and high.strike > low.strike:
                out.append(self._build_put(snapshot, low, high, credit_per_unit, buy_high_ask, sell_low_bid))

        return out

    def _build_call(self, snapshot, low, high, credit_per_unit, buy_low_ask, sell_high_bid):
        lot = snapshot.lot_size
        legs = (
            OpportunityLeg(snapshot.make_instrument(low.strike, OptionType.CALL).id, Side.BUY, lot, buy_low_ask, "long_call_low"),
            OpportunityLeg(snapshot.make_instrument(high.strike, OptionType.CALL).id, Side.SELL, lot, sell_high_bid, "short_call_high"),
        )
        edge_per_lot = credit_per_unit * lot
        max_payoff = (high.strike - low.strike) * lot   # if both ITM at expiry
        return Opportunity(
            kind=OpportunityKind.ADJACENT_STRIKE_CALL,
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            legs=legs,
            theoretical_edge_inr=edge_per_lot,           # locked credit
            edge_per_lot_inr=edge_per_lot,
            edge_after_costs_inr=edge_per_lot - 2 * self.cost_per_leg_inr,
            max_risk_inr=2 * self.cost_per_leg_inr,      # only execution cost; payoff ≥ 0
            capital_required_inr=self.margin_per_lot_estimate_inr,
            notes=(
                f"Call inversion {int(low.strike)}/{int(high.strike)}: "
                f"buy@{buy_low_ask:.2f} sell@{sell_high_bid:.2f} = ₹{credit_per_unit:.2f}/unit credit"
            ),
            extra={"strike_low": low.strike, "strike_high": high.strike, "max_payoff_per_lot": max_payoff},
        )

    def _build_put(self, snapshot, low, high, credit_per_unit, buy_high_ask, sell_low_bid):
        lot = snapshot.lot_size
        legs = (
            OpportunityLeg(snapshot.make_instrument(high.strike, OptionType.PUT).id, Side.BUY, lot, buy_high_ask, "long_put_high"),
            OpportunityLeg(snapshot.make_instrument(low.strike, OptionType.PUT).id, Side.SELL, lot, sell_low_bid, "short_put_low"),
        )
        edge_per_lot = credit_per_unit * lot
        max_payoff = (high.strike - low.strike) * lot
        return Opportunity(
            kind=OpportunityKind.ADJACENT_STRIKE_PUT,
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            legs=legs,
            theoretical_edge_inr=edge_per_lot,
            edge_per_lot_inr=edge_per_lot,
            edge_after_costs_inr=edge_per_lot - 2 * self.cost_per_leg_inr,
            max_risk_inr=2 * self.cost_per_leg_inr,
            capital_required_inr=self.margin_per_lot_estimate_inr,
            notes=(
                f"Put inversion {int(low.strike)}/{int(high.strike)}: "
                f"sell@{sell_low_bid:.2f} buy@{buy_high_ask:.2f} = ₹{credit_per_unit:.2f}/unit credit"
            ),
            extra={"strike_low": low.strike, "strike_high": high.strike, "max_payoff_per_lot": max_payoff},
        )
