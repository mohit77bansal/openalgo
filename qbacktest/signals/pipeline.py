"""Convenience: run the full news → signals pipeline."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.signals.scoring import HeuristicScorer, NewsScoringPipeline, rank_volatile_tomorrow
from qbacktest.signals.sources import (
    BSEAnnouncementSource,
    ECONOMICTIMES_FEEDS,
    GDELTSource,
    MINT_FEEDS,
    MONEYCONTROL_FEEDS,
    RSSSource,
    TwitterSource,
)
from qbacktest.signals.types import NewsItem, VolatilitySignal

log = logging.getLogger(__name__)


async def fetch_all_news(
    *,
    look_back_hours: int = 24,
    include_twitter: bool = False,
    include_gdelt: bool = True,
) -> list[NewsItem]:
    """Pull from every configured source. Returns deduped, time-sorted items."""
    since = datetime.now(IST) - timedelta(hours=look_back_hours)

    sources = [
        RSSSource(feeds=MONEYCONTROL_FEEDS).fetch(since=since),
        RSSSource(feeds=ECONOMICTIMES_FEEDS).fetch(since=since),
        RSSSource(feeds=MINT_FEEDS).fetch(since=since),
        BSEAnnouncementSource().fetch(since=since),
    ]
    if include_gdelt:
        sources.append(GDELTSource().fetch(since=since))
    if include_twitter:
        sources.append(TwitterSource().fetch(since=since))

    results = await asyncio.gather(*sources, return_exceptions=True)
    out: list[NewsItem] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("source error: %s", r)
            continue
        out.extend(r)
    # Dedupe by URL
    seen_urls: set[str] = set()
    deduped: list[NewsItem] = []
    for n in out:
        if n.url in seen_urls:
            continue
        seen_urls.add(n.url)
        deduped.append(n)
    deduped.sort(key=lambda n: n.published_at, reverse=True)
    return deduped


async def run_pipeline(
    *,
    look_back_hours: int = 24,
    realized_moves: Optional[dict[str, float]] = None,
    avg_moves: Optional[dict[str, float]] = None,
    include_twitter: bool = False,
    top_n: int = 10,
    use_llm: bool = True,
    llm_max_items: int = 200,
) -> tuple[list[NewsItem], list[VolatilitySignal]]:
    """Fetch news → score (LLM + heuristic) → return raw items + ranked signals.

    LLM scoring is enabled iff `use_llm=True` AND an LLM API key is configured
    (ANTHROPIC_API_KEY or OPENAI_API_KEY in env). Heuristic scorer is always
    available as fallback.
    """
    news = await fetch_all_news(
        look_back_hours=look_back_hours,
        include_twitter=include_twitter,
    )

    llm_results: dict = {}
    if use_llm:
        from qbacktest.signals.llm_scorer import LLMScorer
        scorer_llm = LLMScorer()
        if scorer_llm.active_provider:
            log.info("Scoring with LLM provider: %s (model: %s)", scorer_llm.active_provider,
                     scorer_llm.anthropic_model if scorer_llm.active_provider == "anthropic"
                     else scorer_llm.openai_model)
            # Cap items sent to LLM to control cost; pick most recent
            head = news[:llm_max_items]
            llm_results = await scorer_llm.score_batch(head)
            log.info("LLM scored %d/%d items", len(llm_results), len(head))
        else:
            log.info("No LLM API key — using heuristic scorer only")

    pipeline = NewsScoringPipeline(
        scorer=HeuristicScorer(),
        realized_moves=realized_moves or {},
        avg_moves=avg_moves or {},
        llm_results=llm_results,
    )
    signals = pipeline.score_news(news)
    top = rank_volatile_tomorrow(signals, top_n=top_n)
    return news, top
