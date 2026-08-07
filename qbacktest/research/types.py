"""Domain types for the research-paper → Indian-feasibility pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class StrategyAssetClass(str, Enum):
    EQUITY = "EQUITY"
    OPTIONS = "OPTIONS"
    FUTURES = "FUTURES"
    FX = "FX"
    FIXED_INCOME = "FIXED_INCOME"
    COMMODITY = "COMMODITY"
    CRYPTO = "CRYPTO"
    MULTI = "MULTI"


class StrategyMarketType(str, Enum):
    US = "US"
    EUROPEAN = "EUROPEAN"
    INDIAN = "INDIAN"
    EMERGING = "EMERGING"
    GLOBAL = "GLOBAL"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True, slots=True)
class PaperRef:
    """Citation metadata for a research paper."""
    arxiv_id: str           # e.g. "2402.01234"
    title: str
    authors: tuple[str, ...]
    abstract: str
    published: datetime
    pdf_url: str
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """LLM-extracted structured spec of a trading strategy."""
    name: str
    asset_class: StrategyAssetClass
    market_type: StrategyMarketType
    signal_summary: str             # 1-2 sentence description
    entry_rule: str                 # plain English
    exit_rule: str
    holding_period: str             # "intraday", "1-5 days", "1 month", etc
    claimed_sharpe: Optional[float] = None
    claimed_annual_return: Optional[float] = None
    requires_short_selling: bool = False
    requires_intraday_data: bool = False
    requires_options_data: bool = False
    requires_microstructure_data: bool = False  # tick-level, order book
    notes: str = ""


@dataclass(frozen=True, slots=True)
class FeasibilityScore:
    """How tradable is this strategy in the Indian market?"""
    overall_score: float           # 0-1
    data_score: float              # do we have the inputs?
    capacity_score: float          # how much capital can be deployed?
    cost_score: float              # does the edge survive Indian costs?
    regulatory_score: float        # SEBI / NSE rules allow it?
    short_borrow_score: float      # if short-selling needed
    blockers: tuple[str, ...] = ()
    notes: str = ""

    @property
    def is_viable(self) -> bool:
        return self.overall_score >= 0.5 and not self.blockers


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """A paper-derived strategy + Indian-feasibility assessment."""
    paper: PaperRef
    spec: StrategySpec
    feasibility: FeasibilityScore
    extracted_at: datetime
    confidence: float = 0.5         # how confident we are in the LLM extraction

    def to_json(self) -> dict:
        return {
            "paper": {
                "arxiv_id": self.paper.arxiv_id,
                "title": self.paper.title,
                "authors": list(self.paper.authors),
                "published": self.paper.published.isoformat(),
                "pdf_url": self.paper.pdf_url,
                "abstract": self.paper.abstract[:600],
            },
            "spec": {
                "name": self.spec.name,
                "asset_class": self.spec.asset_class.value,
                "market_type": self.spec.market_type.value,
                "signal_summary": self.spec.signal_summary,
                "entry_rule": self.spec.entry_rule,
                "exit_rule": self.spec.exit_rule,
                "holding_period": self.spec.holding_period,
                "claimed_sharpe": self.spec.claimed_sharpe,
                "claimed_annual_return": self.spec.claimed_annual_return,
                "requires_short_selling": self.spec.requires_short_selling,
                "requires_intraday_data": self.spec.requires_intraday_data,
                "requires_options_data": self.spec.requires_options_data,
                "requires_microstructure_data": self.spec.requires_microstructure_data,
                "notes": self.spec.notes,
            },
            "feasibility": {
                "overall_score": round(self.feasibility.overall_score, 3),
                "data_score": round(self.feasibility.data_score, 3),
                "capacity_score": round(self.feasibility.capacity_score, 3),
                "cost_score": round(self.feasibility.cost_score, 3),
                "regulatory_score": round(self.feasibility.regulatory_score, 3),
                "short_borrow_score": round(self.feasibility.short_borrow_score, 3),
                "blockers": list(self.feasibility.blockers),
                "notes": self.feasibility.notes,
                "is_viable": self.feasibility.is_viable,
            },
            "confidence": round(self.confidence, 3),
            "extracted_at": self.extracted_at.isoformat(),
        }
