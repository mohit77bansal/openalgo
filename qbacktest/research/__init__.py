"""Research-paper ingestor — find arb/anomaly strategies and assess Indian feasibility.

Pipeline:
    arxiv q-fin.PR / q-fin.TR / SSRN abstracts
        ↓
    Filter: papers proposing TRADING strategies (not pure theory)
        ↓
    LLM: extract structured strategy spec (instrument, signal, holding period, claimed Sharpe)
        ↓
    Indian-feasibility scorer: data availability, lot-size constraints, regulatory blockers,
    short-borrow availability, capacity, transaction-cost survival
        ↓
    StrategyCandidate ranked by (claimed_edge × indian_feasibility)

Public API:
    PaperRef, StrategySpec, StrategyCandidate
    fetch_arxiv_finance_papers
    extract_strategy_with_llm
    score_indian_feasibility
"""

from qbacktest.research.types import (
    PaperRef,
    StrategySpec,
    StrategyCandidate,
    FeasibilityScore,
)
from qbacktest.research.arxiv_source import fetch_arxiv_finance_papers
from qbacktest.research.feasibility import score_indian_feasibility

__all__ = [
    "FeasibilityScore",
    "PaperRef",
    "StrategyCandidate",
    "StrategySpec",
    "fetch_arxiv_finance_papers",
    "score_indian_feasibility",
]
