"""News-driven trading signals for Indian equities.

Pipeline:
    Sources (Moneycontrol RSS, GDELT, Twitter, Nitter) → NewsItem stream
        ↓
    Entity extraction (NSE-listed company / sector / index)
        ↓
    Sentiment + event classifier (LLM if key available, heuristic otherwise)
        ↓
    Reaction estimator (realized vol today vs 30d avg → already-priced or pending)
        ↓
    VolatilitySignal with: ticker, direction, magnitude, time horizon, confidence

Public API:
    NewsItem
    VolatilitySignal
    ingest_news(...)        — pull from all configured sources
    score_news(...)          — entity + sentiment + reaction
    rank_volatile_tomorrow() — top-N likely-to-move-tomorrow tickers
"""

from qbacktest.signals.types import (
    NewsItem,
    VolatilitySignal,
    Sentiment,
    EventKind,
    ReactionState,
)

__all__ = [
    "EventKind",
    "NewsItem",
    "ReactionState",
    "Sentiment",
    "VolatilitySignal",
]
