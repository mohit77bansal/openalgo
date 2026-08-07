"""End-to-end research pipeline: arxiv → strategies → Indian feasibility."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.research.arxiv_source import fetch_arxiv_finance_papers
from qbacktest.research.feasibility import score_indian_feasibility
from qbacktest.research.llm_extract import extract_strategy_with_llm
from qbacktest.research.types import (
    PaperRef,
    StrategyCandidate,
    StrategySpec,
    StrategyAssetClass,
    StrategyMarketType,
    FeasibilityScore,
)

log = logging.getLogger(__name__)


async def run_research_pipeline(
    *,
    days_back: int = 60,
    max_papers: int = 50,
    use_llm: bool = True,
) -> tuple[list[PaperRef], list[StrategyCandidate]]:
    """Pull recent arxiv finance papers → extract strategies → score feasibility."""
    papers = await fetch_arxiv_finance_papers(days_back=days_back, max_results=max_papers)
    log.info("arxiv: fetched %d candidate papers", len(papers))

    candidates: list[StrategyCandidate] = []
    if not papers:
        return papers, candidates

    if use_llm:
        sem = asyncio.Semaphore(5)   # cap LLM concurrency

        async def _process(p: PaperRef):
            async with sem:
                spec: Optional[StrategySpec] = None
                try:
                    spec = await extract_strategy_with_llm(p)
                except Exception as e:
                    log.warning("LLM extract failed for %s: %s", p.arxiv_id, e)
                if spec is None:
                    return None
                feas = score_indian_feasibility(spec)
                return StrategyCandidate(
                    paper=p,
                    spec=spec,
                    feasibility=feas,
                    extracted_at=datetime.now(IST),
                    confidence=0.7,
                )

        results = await asyncio.gather(*[_process(p) for p in papers])
        candidates = [r for r in results if r is not None]
    else:
        # No-LLM fallback: emit lightweight specs from titles only — useful for demo
        for p in papers:
            spec = StrategySpec(
                name=p.title[:60],
                asset_class=StrategyAssetClass.MULTI,
                market_type=StrategyMarketType.UNSPECIFIED,
                signal_summary=p.abstract[:200],
                entry_rule="see paper",
                exit_rule="see paper",
                holding_period="unspecified",
                notes="No LLM extraction — set ANTHROPIC_API_KEY/OPENAI_API_KEY for structured spec.",
            )
            feas = score_indian_feasibility(spec)
            candidates.append(StrategyCandidate(
                paper=p,
                spec=spec,
                feasibility=feas,
                extracted_at=datetime.now(IST),
                confidence=0.2,
            ))

    candidates.sort(
        key=lambda c: (c.feasibility.overall_score, c.spec.claimed_sharpe or 0),
        reverse=True,
    )
    return papers, candidates
