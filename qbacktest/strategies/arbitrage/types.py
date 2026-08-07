"""Common types for arbitrage scanners."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

from qbacktest.core.types import Side


class OpportunityKind(str, Enum):
    BOX_SPREAD = "BOX_SPREAD"
    CONVERSION = "CONVERSION"
    REVERSAL = "REVERSAL"
    ADJACENT_STRIKE_CALL = "ADJACENT_STRIKE_CALL"
    ADJACENT_STRIKE_PUT = "ADJACENT_STRIKE_PUT"
    PCP_CALL_RICH = "PCP_CALL_RICH"
    PCP_PUT_RICH = "PCP_PUT_RICH"
    FAR_OTM_PREMIUM = "FAR_OTM_PREMIUM"


@dataclass(frozen=True, slots=True)
class OpportunityLeg:
    """A single leg of an arbitrage construct."""

    instrument_id: str        # e.g. "NFO:NIFTY24DEC25000CE"
    side: Side
    quantity: int             # in units (lots × lot_size)
    price: float              # mid or chosen execution price
    leg_type: str = ""        # human-readable: "long_call_low_strike" etc.


@dataclass(frozen=True, slots=True)
class Opportunity:
    """An identified arbitrage or quasi-arbitrage opportunity.

    `theoretical_edge_inr` is the edge before costs. `edge_after_costs_inr` is
    the same minus an estimated round-trip cost (brokerage + STT + exchange + GST
    + stamp duty for the legs).

    `max_risk_inr` is the worst-case loss under reasonable assumptions:
        - For pure arbs (box, parity, conversion, adjacent): execution risk only
          (legging slippage). max_risk = a worst-case slippage estimate.
        - For quasi-arbs (far-OTM, calendar): real loss possible — populated.

    `capital_required_inr` is the SPAN/exposure margin estimate (offsets applied)
    for holding the position.
    """

    kind: OpportunityKind
    underlying: str
    expiry: date
    legs: tuple[OpportunityLeg, ...]
    theoretical_edge_inr: float
    edge_per_lot_inr: float
    max_risk_inr: float
    capital_required_inr: float
    edge_after_costs_inr: float = 0.0
    notes: str = ""
    extra: dict[str, object] = field(default_factory=dict, hash=False)

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "legs": [
                {
                    "instrument": leg.instrument_id,
                    "side": leg.side.value,
                    "quantity": leg.quantity,
                    "price": leg.price,
                    "leg_type": leg.leg_type,
                }
                for leg in self.legs
            ],
            "theoretical_edge_inr": round(self.theoretical_edge_inr, 2),
            "edge_per_lot_inr": round(self.edge_per_lot_inr, 2),
            "edge_after_costs_inr": round(self.edge_after_costs_inr, 2),
            "max_risk_inr": round(self.max_risk_inr, 2),
            "capital_required_inr": round(self.capital_required_inr, 2),
            "notes": self.notes,
        }
