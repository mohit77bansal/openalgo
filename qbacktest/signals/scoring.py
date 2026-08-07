"""Sentiment + event-kind classifier (heuristic).

Pure rule-based scorer that works WITHOUT any LLM API key. It's not state-of-
the-art but it's transparent, reproducible, and good enough for "screen these
five stocks tomorrow morning."

For richer scoring, swap `score_news_heuristic` for an LLM-backed implementation
(see `score_news_llm` stub).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from qbacktest.signals.entities import EntityMatch, extract_entities, expand_to_sectors
from qbacktest.signals.types import (
    EventKind,
    NewsItem,
    ReactionState,
    Sentiment,
    VolatilitySignal,
)


# --- Keyword heuristics ----------------------------------------------------- #

_POSITIVE_TERMS = {
    "beat", "beats", "surge", "soar", "jump", "rally", "record",
    "raise guidance", "raises guidance", "upgraded", "buy rating", "outperform",
    "wins order", "bags order", "secures contract", "receives approval",
    "stake hike", "buyback", "bonus issue", "stock split", "dividend declared",
    "expansion", "merger approved", "deal closed",
}
_NEGATIVE_TERMS = {
    "miss", "misses", "plunge", "fall", "drop", "tumble", "slump", "crash",
    "cut guidance", "cuts guidance", "downgraded", "sell rating",
    "loses contract", "scrip ban", "f&o ban", "regulatory action",
    "probe", "investigation", "raid", "tax demand", "fine", "penalty",
    "fraud", "default", "bankruptcy", "stake sale", "promoter pledge",
    "downgrade", "loss widens", "guidance cut", "qualification", "going concern",
}

# event_kind: (regex, kind, default_magnitude_pct, horizon_days)
_EVENT_PATTERNS: list[tuple[re.Pattern, EventKind, float, int]] = [
    (re.compile(r"\b(quarter(ly)? results?|q[1-4]\s*results?|earning(s)?)\b", re.I), EventKind.EARNINGS_BEAT, 3.0, 1),
    (re.compile(r"\b(beats?|tops?)\s+(\S+\s+){0,3}(estimates?|consensus|expectations?)\b", re.I), EventKind.EARNINGS_BEAT, 4.0, 1),
    (re.compile(r"\b(misses?|below)\s+(\S+\s+){0,3}(estimates?|consensus|expectations?)\b", re.I), EventKind.EARNINGS_MISS, 4.0, 1),
    (re.compile(r"\b(rais(es|ed)|hikes?)\s+guidance\b", re.I), EventKind.GUIDANCE_RAISE, 5.0, 2),
    (re.compile(r"\b(cuts?|lower(s|ed))\s+guidance\b", re.I), EventKind.GUIDANCE_CUT, 5.0, 2),
    (re.compile(r"\b(merger|acquir(es|ed|ing)|takeover|to\s+acquire)\b", re.I), EventKind.ACQUISITION, 6.0, 3),
    (re.compile(r"\bspin[\s-]?off\b", re.I), EventKind.SPINOFF, 4.0, 5),
    (re.compile(r"\b(SEBI|RBI|CCI|CCIE)\s+(probe|order|action|notice)\b", re.I), EventKind.REGULATORY_ACTION, 6.0, 3),
    (re.compile(r"\b(buyback|share\s+repurchase)\b", re.I), EventKind.BUYBACK, 3.0, 5),
    (re.compile(r"\bdividend\s+(declared|announced|of)\b", re.I), EventKind.DIVIDEND, 1.5, 1),
    (re.compile(r"\b(stock\s+split|bonus\s+issue)\b", re.I), EventKind.SPLIT_BONUS, 2.5, 3),
    (re.compile(r"\b(block\s+deal|bulk\s+deal)\b", re.I), EventKind.BLOCK_DEAL, 2.0, 1),
    (re.compile(r"\b(monetary\s+policy|repo\s+rate|crr)\b", re.I), EventKind.RBI_POLICY, 1.5, 2),
    (re.compile(r"\b(union\s+budget|budget\s+\d{4})\b", re.I), EventKind.BUDGET, 2.0, 3),
    (re.compile(r"\b(launches?|product\s+launch|new\s+product)\b", re.I), EventKind.PRODUCT_LAUNCH, 2.0, 3),
    (re.compile(r"\b(CEO|MD|chairman|managing\s+director)\s+(steps?\s+down|resigns?|appointed|to\s+exit)\b", re.I),
        EventKind.MANAGEMENT_CHANGE, 4.0, 2),
    (re.compile(r"\b(wins?|secures?|bags?)\s+(order|contract|deal)\b", re.I), EventKind.LARGE_ORDER, 3.0, 1),
    (re.compile(r"\b(lawsuit|sued|files?\s+suit)\b", re.I), EventKind.LITIGATION, 3.5, 5),
]


@dataclass
class HeuristicScorer:
    """Rule-based sentiment + event classifier."""

    def score_one(self, news: NewsItem) -> tuple[Sentiment, EventKind, float, int, float]:
        """Return (sentiment, event_kind, expected_move_pct, horizon_days, confidence)."""
        text = f"{news.title} {news.body_excerpt}".lower()

        # Sentiment by keyword counts
        pos = sum(1 for term in _POSITIVE_TERMS if term in text)
        neg = sum(1 for term in _NEGATIVE_TERMS if term in text)
        if pos > neg:
            sentiment = Sentiment.POSITIVE
        elif neg > pos:
            sentiment = Sentiment.NEGATIVE
        elif pos > 0 and neg > 0:
            sentiment = Sentiment.MIXED
        else:
            sentiment = Sentiment.NEUTRAL

        # Event kind by regex
        event_kind = EventKind.GENERIC
        magnitude = 1.0
        horizon = 1
        for pat, kind, mag, h in _EVENT_PATTERNS:
            if pat.search(news.title) or pat.search(news.body_excerpt):
                event_kind = kind
                magnitude = mag
                horizon = h
                break

        # Confidence: more keywords + matched event = higher
        confidence = min(1.0, 0.3 + 0.15 * (pos + neg) + (0.3 if event_kind != EventKind.GENERIC else 0))

        # Adjust expected move when sentiment is strong
        if sentiment in (Sentiment.POSITIVE, Sentiment.NEGATIVE) and event_kind == EventKind.GENERIC:
            magnitude = max(magnitude, 1.5)

        return sentiment, event_kind, magnitude, horizon, confidence


# --- Reaction state estimator ---------------------------------------------- #


def estimate_reaction(
    *,
    expected_move_pct: float,
    realized_move_today_pct: Optional[float],
    avg_daily_move_pct: Optional[float],
) -> ReactionState:
    """Compare today's realized move to expected. Coarse heuristic."""
    if realized_move_today_pct is None or avg_daily_move_pct is None:
        return ReactionState.NOT_YET
    realized = abs(realized_move_today_pct)
    if expected_move_pct <= 0:
        return ReactionState.NOT_YET
    ratio = realized / expected_move_pct
    if ratio >= 1.5:
        return ReactionState.OVERSHOT
    if ratio >= 0.8:
        return ReactionState.FULLY
    if ratio >= 0.3:
        return ReactionState.PARTIALLY
    return ReactionState.NOT_YET


# --- Direction inference ---------------------------------------------------- #


def expected_direction_from(sentiment: Sentiment, event: EventKind) -> str:
    if sentiment == Sentiment.POSITIVE:
        return "up"
    if sentiment == Sentiment.NEGATIVE:
        return "down"
    # Mixed / neutral but with a known event → either direction (straddle play)
    if event != EventKind.GENERIC:
        return "either"
    return "either"


# --- Pipeline --------------------------------------------------------------- #


@dataclass
class NewsScoringPipeline:
    """End-to-end: news → entities → score → VolatilitySignal.

    If `llm_results` is provided (map: news.url → LLMResult), the LLM's
    extracted tickers/sentiment/event are used. Heuristic acts as fallback for
    any item the LLM didn't cover (or if no LLM key is configured).
    """

    scorer: HeuristicScorer
    realized_moves: dict[str, float] = None       # ticker → today's % move
    avg_moves: dict[str, float] = None            # ticker → 30d avg daily |%|
    llm_results: dict | None = None               # url -> LLMResult (optional)

    def score_news(self, items: Iterable[NewsItem]) -> list[VolatilitySignal]:
        signals: list[VolatilitySignal] = []
        items_list = list(items)
        # Group items by ticker. Prefer LLM-extracted tickers when available,
        # else fall back to heuristic regex extraction.
        by_ticker: dict[str, list[NewsItem]] = {}
        llm_per_url: dict[str, dict] = {}
        for it in items_list:
            tickers: list[str] = []
            llm_r = (self.llm_results or {}).get(it.url)
            if llm_r is not None:
                tickers = list(llm_r.tickers) if hasattr(llm_r, "tickers") else []
                llm_per_url[it.url] = llm_r
            if not tickers:
                ents = extract_entities(it.title + " " + it.body_excerpt)
                tickers = [e.ticker for e in ents]
            for tk in tickers:
                by_ticker.setdefault(tk, []).append(it)

        for ticker, news_list in by_ticker.items():
            news_list.sort(key=lambda n: n.published_at, reverse=True)
            primary = news_list[0]
            llm_primary = llm_per_url.get(primary.url)
            if llm_primary is not None:
                sentiment = llm_primary.sentiment
                event_kind = llm_primary.event_kind
                move = llm_primary.expected_move_pct
                horizon = llm_primary.horizon_days
                conf = llm_primary.confidence
                source_tag = "llm"
            else:
                sentiment, event_kind, move, horizon, conf = self.scorer.score_one(primary)
                source_tag = "heuristic"
            if len(news_list) > 1:
                conf = min(1.0, conf + 0.1 * (len(news_list) - 1))

            realized = self.realized_moves.get(ticker) if self.realized_moves else None
            avg = self.avg_moves.get(ticker) if self.avg_moves else None
            reaction = estimate_reaction(
                expected_move_pct=move,
                realized_move_today_pct=realized,
                avg_daily_move_pct=avg,
            )
            direction = expected_direction_from(sentiment, event_kind)

            llm_reasoning = (llm_primary.reasoning if llm_primary is not None else "")
            note = f"[{source_tag}] {len(news_list)} item(s)"
            if llm_reasoning:
                note += f" — {llm_reasoning}"

            signal = VolatilitySignal(
                ticker=ticker,
                underlying_kind=("index" if ticker in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
                                 else "stock"),
                sentiment=sentiment,
                event_kind=event_kind,
                reaction_state=reaction,
                expected_move_pct=move,
                expected_direction=direction,
                horizon_days=horizon,
                confidence=conf,
                primary_news=primary,
                supporting_news=tuple(news_list[1:6]),
                realized_move_today_pct=realized,
                avg_daily_move_pct=avg,
                notes=note,
            )
            signals.append(signal)

        signals.sort(key=lambda s: (s.likely_volatile_tomorrow, s.confidence, s.expected_move_pct), reverse=True)
        return signals


def rank_volatile_tomorrow(signals: list[VolatilitySignal], top_n: int = 10) -> list[VolatilitySignal]:
    """Filter to signals likely to move tomorrow + rank."""
    candidates = [s for s in signals if s.likely_volatile_tomorrow]
    candidates.sort(key=lambda s: s.confidence * s.expected_move_pct, reverse=True)
    return candidates[:top_n]


# --- LLM-backed alternative (stub) ----------------------------------------- #


def score_news_llm(news: NewsItem) -> tuple[Sentiment, EventKind, float, int, float]:
    """LLM scorer placeholder. Wire to OpenAI/Anthropic via httpx if key set.

    Not implemented in v1 — heuristic scorer is the default. To enable, set
    `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) and replace the call site to use
    this function instead of `HeuristicScorer.score_one`.
    """
    raise NotImplementedError("LLM scorer is a stub — set OPENAI_API_KEY and implement.")
