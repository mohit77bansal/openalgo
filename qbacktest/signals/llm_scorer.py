"""LLM-backed news scorer.

Uses Anthropic Claude (preferred) or OpenAI to do entity extraction +
sentiment + event classification + reaction estimation in one structured pass.

Why LLM beats the heuristic:
- Picks up paraphrases ("supply-chain disruption" = NEGATIVE without keyword match)
- Disambiguates "Bajaj Auto" vs "Bajaj Finance" mentions
- Reads context: "muted Q4 but management upbeat for FY26" → MIXED
- Distinguishes "RBI raises rates" (rate-sensitive sectors fall) from
  "RBI cuts rates" (banks/NBFCs rise)

Cost control:
- Batches up to 20 headlines per LLM call
- Uses cheap models by default (claude-haiku, gpt-4o-mini)
- Caches results by URL hash (subsequent runs are free)

Set ONE of:
    ANTHROPIC_API_KEY=sk-ant-…
    OPENAI_API_KEY=sk-…

Falls back to HeuristicScorer cleanly if neither is present.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Optional

import httpx

from qbacktest.signals.scoring import HeuristicScorer
from qbacktest.signals.types import EventKind, NewsItem, Sentiment

log = logging.getLogger(__name__)


_LLM_PROMPT = """You are a tagger for Indian equity-market news. For each headline, output a single JSON object inside a JSON array. Maintain order.

Indian-market context:
- F&O tickers are NSE symbols (e.g. RELIANCE, HDFCBANK, BAJAJ-AUTO).
- Indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX.
- "Q4 results" usually = announcement of Jan-Mar quarter earnings.
- "Block deal" / "bulk deal" = forced supply, often negative short-term.
- RBI rate hikes hurt banks/NBFCs (margin compression in long term, but cushion via reset on the way up); rate cuts help.
- SEBI orders against a company are usually NEGATIVE.

For each headline, output exactly:
{
  "tickers": ["NSE_SYMBOL", ...],          // 0..n NSE F&O tickers most affected; [] if pure macro
  "sentiment": "POSITIVE"|"NEGATIVE"|"NEUTRAL"|"MIXED",
  "event_kind": "EARNINGS_BEAT"|"EARNINGS_MISS"|"GUIDANCE_RAISE"|"GUIDANCE_CUT"|"MERGER"|"ACQUISITION"|"SPINOFF"|"REGULATORY_ACTION"|"LITIGATION"|"PRODUCT_LAUNCH"|"LARGE_ORDER"|"MANAGEMENT_CHANGE"|"BUYBACK"|"DIVIDEND"|"SPLIT_BONUS"|"BLOCK_DEAL"|"RBI_POLICY"|"BUDGET"|"GENERIC",
  "expected_move_pct": <number 0-15>,      // expected magnitude on the named tickers
  "expected_direction": "up"|"down"|"either",
  "horizon_days": <int 1-7>,               // when the move plays out
  "confidence": <number 0-1>,              // your confidence
  "reasoning": "<one short sentence>"
}

Be SPECIFIC. If the headline is about a sector, list the constituent tickers, not the sector word. If it's pure noise (cricket, weather, generic India macro), set tickers=[] and event_kind=GENERIC. If you can't tell, use NEUTRAL/GENERIC/0.5 confidence.

Headlines:"""


@dataclass
class LLMResult:
    tickers: list[str]
    sentiment: Sentiment
    event_kind: EventKind
    expected_move_pct: float
    expected_direction: str
    horizon_days: int
    confidence: float
    reasoning: str

    def fallback_to_heuristic(self) -> bool:
        return self.confidence < 0.05


@dataclass
class LLMScorer:
    """Scores news with Anthropic Claude (preferred) or OpenAI.

    Falls back to heuristic scorer if no API key is present, ensuring the
    pipeline always works.
    """

    provider: Literal["anthropic", "openai", "auto"] = "auto"
    anthropic_model: str = "claude-haiku-4-5-20251001"
    openai_model: str = "gpt-4o-mini"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "qbacktest-llm")
    batch_size: int = 20
    timeout: float = 60.0
    heuristic_fallback: HeuristicScorer = field(default_factory=HeuristicScorer)

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_db = self.cache_dir / "scorer.sqlite"
        with sqlite3.connect(self._cache_db) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS scores ("
                " key TEXT PRIMARY KEY, payload TEXT, created_at TEXT)"
            )

    @property
    def active_provider(self) -> Optional[Literal["anthropic", "openai"]]:
        if self.provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if self.provider == "openai" and os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if self.provider == "auto":
            if os.environ.get("ANTHROPIC_API_KEY"):
                return "anthropic"
            if os.environ.get("OPENAI_API_KEY"):
                return "openai"
        return None

    # --- Public API ---------------------------------------------------- #

    async def score_batch(self, items: Iterable[NewsItem]) -> dict[str, LLMResult]:
        """Score a batch of news items. Returns map: news.url → LLMResult.

        Items missing from the result map should be scored via heuristic.
        """
        items_list = list(items)
        if not items_list:
            return {}
        provider = self.active_provider
        if provider is None:
            log.info("No LLM API key — using heuristic scorer for everything")
            return {}

        # Cache lookup
        results: dict[str, LLMResult] = {}
        to_score: list[NewsItem] = []
        for n in items_list:
            cached = self._cache_get(n)
            if cached is not None:
                results[n.url] = cached
            else:
                to_score.append(n)

        if not to_score:
            return results

        # Batch into groups
        for i in range(0, len(to_score), self.batch_size):
            chunk = to_score[i : i + self.batch_size]
            try:
                if provider == "anthropic":
                    chunk_results = await self._score_anthropic(chunk)
                else:
                    chunk_results = await self._score_openai(chunk)
                for n, r in zip(chunk, chunk_results):
                    if r is not None:
                        results[n.url] = r
                        self._cache_put(n, r)
            except Exception as e:
                log.warning("LLM batch failed (%s) — heuristic fallback for %d items", e, len(chunk))
        return results

    # --- Anthropic ----------------------------------------------------- #

    async def _score_anthropic(self, batch: list[NewsItem]) -> list[Optional[LLMResult]]:
        api_key = os.environ["ANTHROPIC_API_KEY"]
        prompt = _LLM_PROMPT + "\n\n" + _format_batch(batch) + "\n\nRespond ONLY with the JSON array, no prose."
        body = {
            "model": self.anthropic_model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                                  json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        text = data["content"][0]["text"]
        return _parse_llm_array(text, expected=len(batch))

    # --- OpenAI -------------------------------------------------------- #

    async def _score_openai(self, batch: list[NewsItem]) -> list[Optional[LLMResult]]:
        api_key = os.environ["OPENAI_API_KEY"]
        prompt = _LLM_PROMPT + "\n\n" + _format_batch(batch)
        body = {
            "model": self.openai_model,
            "messages": [
                {"role": "system", "content": "Output only valid JSON arrays. No prose."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        return _parse_llm_array(text, expected=len(batch))

    # --- Cache --------------------------------------------------------- #

    def _key(self, n: NewsItem) -> str:
        h = hashlib.sha1(n.url.encode("utf-8")).hexdigest()
        return f"{h}:{n.title[:80]}"

    def _cache_get(self, n: NewsItem) -> Optional[LLMResult]:
        with sqlite3.connect(self._cache_db) as c:
            row = c.execute("SELECT payload FROM scores WHERE key = ?", (self._key(n),)).fetchone()
        if not row:
            return None
        try:
            d = json.loads(row[0])
            return LLMResult(**{**d, "sentiment": Sentiment(d["sentiment"]), "event_kind": EventKind(d["event_kind"])})
        except Exception:
            return None

    def _cache_put(self, n: NewsItem, r: LLMResult) -> None:
        with sqlite3.connect(self._cache_db) as c:
            c.execute(
                "INSERT OR REPLACE INTO scores VALUES (?, ?, ?)",
                (self._key(n), json.dumps({
                    "tickers": r.tickers,
                    "sentiment": r.sentiment.value,
                    "event_kind": r.event_kind.value,
                    "expected_move_pct": r.expected_move_pct,
                    "expected_direction": r.expected_direction,
                    "horizon_days": r.horizon_days,
                    "confidence": r.confidence,
                    "reasoning": r.reasoning,
                }), datetime.utcnow().isoformat()),
            )


# --- Helpers ---------------------------------------------------------- #


def _format_batch(items: list[NewsItem]) -> str:
    lines = []
    for i, n in enumerate(items, start=1):
        body = (n.body_excerpt or "").strip()[:300]
        lines.append(f"[{i}] {n.source} | {n.published_at:%Y-%m-%d %H:%M} | {n.title}"
                     + (f"\n    {body}" if body else ""))
    return "\n".join(lines)


def _parse_llm_array(text: str, *, expected: int) -> list[Optional[LLMResult]]:
    """Parse the JSON array from LLM output. Tolerant to wrappers & prose."""
    text = text.strip()
    # Find first [ and matching ]
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        log.warning("LLM response had no JSON array — got: %s", text[:200])
        return [None] * expected
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        log.warning("LLM JSON parse error: %s — body: %s", e, text[:200])
        return [None] * expected
    out: list[Optional[LLMResult]] = []
    for d in data:
        try:
            out.append(LLMResult(
                tickers=list(d.get("tickers", [])),
                sentiment=Sentiment(d.get("sentiment", "NEUTRAL")),
                event_kind=EventKind(d.get("event_kind", "GENERIC")),
                expected_move_pct=float(d.get("expected_move_pct", 1.0)),
                expected_direction=str(d.get("expected_direction", "either")),
                horizon_days=int(d.get("horizon_days", 1)),
                confidence=float(d.get("confidence", 0.5)),
                reasoning=str(d.get("reasoning", "")),
            ))
        except (ValueError, KeyError) as e:
            log.debug("LLM row skip: %s", e)
            out.append(None)
    # Pad / trim to expected length
    while len(out) < expected:
        out.append(None)
    return out[:expected]
