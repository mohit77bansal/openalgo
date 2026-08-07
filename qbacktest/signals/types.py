"""Domain types for the news → volatility-signal pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Sentiment(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"


class EventKind(str, Enum):
    """The flavor of news event. Drives expected vol shape."""
    EARNINGS_BEAT = "EARNINGS_BEAT"
    EARNINGS_MISS = "EARNINGS_MISS"
    GUIDANCE_RAISE = "GUIDANCE_RAISE"
    GUIDANCE_CUT = "GUIDANCE_CUT"
    MERGER = "MERGER"
    ACQUISITION = "ACQUISITION"
    SPINOFF = "SPINOFF"
    REGULATORY_ACTION = "REGULATORY_ACTION"
    LITIGATION = "LITIGATION"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    LARGE_ORDER = "LARGE_ORDER"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    BUYBACK = "BUYBACK"
    DIVIDEND = "DIVIDEND"
    SPLIT_BONUS = "SPLIT_BONUS"
    BLOCK_DEAL = "BLOCK_DEAL"
    RBI_POLICY = "RBI_POLICY"        # macro — moves index, not single stocks
    BUDGET = "BUDGET"
    GENERIC = "GENERIC"


class ReactionState(str, Enum):
    """Has the market already digested this news?"""
    NOT_YET = "NOT_YET"          # news fresh, market closed/limited reaction
    PARTIALLY = "PARTIALLY"      # some movement seen, room remains
    FULLY = "FULLY"              # priced in, low residual vol expected
    OVERSHOT = "OVERSHOT"        # stock overreacted, possible mean-reversion play


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A single news headline pulled from a source."""
    source: str                  # e.g. "moneycontrol_rss", "twitter", "gdelt"
    title: str
    url: str
    published_at: datetime       # timezone-aware
    body_excerpt: str = ""
    raw: dict = field(default_factory=dict, hash=False)

    @property
    def short(self) -> str:
        return self.title[:100] + ("…" if len(self.title) > 100 else "")


@dataclass(frozen=True, slots=True)
class VolatilitySignal:
    """A scored signal that a specific instrument may move."""
    ticker: str                  # NSE symbol, e.g. "RELIANCE", or "NIFTY" for index
    underlying_kind: str         # "stock" | "index" | "sector"
    sentiment: Sentiment
    event_kind: EventKind
    reaction_state: ReactionState
    expected_move_pct: float     # rough magnitude estimate (positive number; sentiment gives direction)
    expected_direction: str      # "up" | "down" | "either" (straddle play)
    horizon_days: int            # over how many sessions the move should play out
    confidence: float            # 0-1; how strong the signal is
    primary_news: NewsItem
    supporting_news: tuple[NewsItem, ...] = ()
    realized_move_today_pct: Optional[float] = None
    avg_daily_move_pct: Optional[float] = None
    notes: str = ""

    @property
    def already_reacted(self) -> bool:
        return self.reaction_state in (ReactionState.FULLY, ReactionState.OVERSHOT)

    @property
    def likely_volatile_tomorrow(self) -> bool:
        return self.reaction_state in (ReactionState.NOT_YET, ReactionState.PARTIALLY)

    @property
    def trade_suggestion(self) -> str:
        """Human-readable suggestion. NOT financial advice."""
        if self.reaction_state == ReactionState.NOT_YET:
            if self.expected_direction == "up":
                return f"BUY ATM/OTM call on {self.ticker} at open; size for {self.expected_move_pct:.1f}% move"
            if self.expected_direction == "down":
                return f"BUY ATM/OTM put on {self.ticker} at open; size for {self.expected_move_pct:.1f}% move"
            return f"BUY ATM straddle on {self.ticker} (direction uncertain, magnitude {self.expected_move_pct:.1f}%)"
        if self.reaction_state == ReactionState.PARTIALLY:
            return f"Watch {self.ticker} for residual {self.expected_move_pct:.1f}% move; smaller size"
        if self.reaction_state == ReactionState.OVERSHOT:
            return f"{self.ticker} overshot on news; consider mean-reversion (sell premium)"
        return f"{self.ticker} fully priced in — skip"

    def to_json(self) -> dict:
        return {
            "ticker": self.ticker,
            "underlying_kind": self.underlying_kind,
            "sentiment": self.sentiment.value,
            "event_kind": self.event_kind.value,
            "reaction_state": self.reaction_state.value,
            "expected_move_pct": round(self.expected_move_pct, 3),
            "expected_direction": self.expected_direction,
            "horizon_days": self.horizon_days,
            "confidence": round(self.confidence, 3),
            "realized_move_today_pct": (
                round(self.realized_move_today_pct, 3) if self.realized_move_today_pct is not None else None
            ),
            "avg_daily_move_pct": (
                round(self.avg_daily_move_pct, 3) if self.avg_daily_move_pct is not None else None
            ),
            "primary_news": {
                "source": self.primary_news.source,
                "title": self.primary_news.title,
                "url": self.primary_news.url,
                "published_at": self.primary_news.published_at.isoformat(),
            },
            "supporting_count": len(self.supporting_news),
            "trade_suggestion": self.trade_suggestion,
            "notes": self.notes,
        }
