"""Arbitrage scanners for the Indian options market.

Each scanner takes a `ChainSnapshot` and returns a list of `Opportunity` objects
describing the structure, theoretical edge, capital requirement, and risk.

See `docs/RESEARCH.md` for the theory behind each strategy and why retail can
capture these specifically.

Public API:
    Opportunity, OpportunityLeg
    BoxSpreadScanner
    ConversionReversalScanner
    AdjacentStrikeInversionScanner
    PutCallParityScanner
    FarOTMPremiumScanner       (flagged with mandatory hedge requirement)
    scan_all(snapshot)         convenience: runs every scanner
"""

from qbacktest.strategies.arbitrage.types import Opportunity, OpportunityLeg
from qbacktest.strategies.arbitrage.box_spread import BoxSpreadScanner
from qbacktest.strategies.arbitrage.conversion import ConversionReversalScanner
from qbacktest.strategies.arbitrage.adjacent_strike import AdjacentStrikeInversionScanner
from qbacktest.strategies.arbitrage.parity import PutCallParityScanner
from qbacktest.strategies.arbitrage.far_otm import FarOTMPremiumScanner
from qbacktest.strategies.arbitrage.scan_all import scan_all

__all__ = [
    "AdjacentStrikeInversionScanner",
    "BoxSpreadScanner",
    "ConversionReversalScanner",
    "FarOTMPremiumScanner",
    "Opportunity",
    "OpportunityLeg",
    "PutCallParityScanner",
    "scan_all",
]
