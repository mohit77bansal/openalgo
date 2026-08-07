"""Convenience: run every scanner over a snapshot."""

from __future__ import annotations

from qbacktest.core.chain import ChainSnapshot
from qbacktest.strategies.arbitrage.adjacent_strike import AdjacentStrikeInversionScanner
from qbacktest.strategies.arbitrage.box_spread import BoxSpreadScanner
from qbacktest.strategies.arbitrage.conversion import ConversionReversalScanner
from qbacktest.strategies.arbitrage.far_otm import FarOTMPremiumScanner
from qbacktest.strategies.arbitrage.parity import PutCallParityScanner
from qbacktest.strategies.arbitrage.types import Opportunity


def scan_all(
    snapshot: ChainSnapshot,
    *,
    include_far_otm: bool = False,
) -> list[Opportunity]:
    """Run all pure-arb scanners + optionally the quasi-arb FarOTM scanner.

    Returns opportunities sorted by `edge_after_costs_inr` descending.
    """
    out: list[Opportunity] = []
    out.extend(BoxSpreadScanner().scan(snapshot))
    out.extend(PutCallParityScanner().scan(snapshot))
    out.extend(ConversionReversalScanner().scan(snapshot))
    out.extend(AdjacentStrikeInversionScanner().scan(snapshot))
    if include_far_otm:
        out.extend(FarOTMPremiumScanner().scan(snapshot))
    out.sort(key=lambda o: o.edge_after_costs_inr, reverse=True)
    return out
