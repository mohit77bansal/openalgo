"""LLM-backed strategy extraction from paper abstracts.

Given a PaperRef (title + abstract), ask the LLM to:
1. Decide if this paper proposes a tradable strategy (vs pure theory).
2. Extract a structured StrategySpec.
3. Note its assumed market.

Skip papers that don't propose strategies (LLM returns null).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

from qbacktest.research.types import (
    PaperRef,
    StrategyAssetClass,
    StrategyMarketType,
    StrategySpec,
)

log = logging.getLogger(__name__)


_EXTRACT_PROMPT = """You are a quant analyst. Read the title and abstract of a finance research paper. Decide:
1. Does this paper propose a TRADABLE strategy? (Pure theory or surveys → no.)
2. If yes, extract structured fields below.

Output ONLY a JSON object. If not tradable, output: {"tradable": false}.

Otherwise:
{
  "tradable": true,
  "name": "<short label, ≤6 words>",
  "asset_class": "EQUITY"|"OPTIONS"|"FUTURES"|"FX"|"FIXED_INCOME"|"COMMODITY"|"CRYPTO"|"MULTI",
  "market_type": "US"|"EUROPEAN"|"INDIAN"|"EMERGING"|"GLOBAL"|"UNSPECIFIED",
  "signal_summary": "<1-2 sentences in plain English describing the alpha source>",
  "entry_rule": "<concrete entry condition>",
  "exit_rule": "<concrete exit condition>",
  "holding_period": "intraday"|"1-5 days"|"1-4 weeks"|"1-12 months"|"unspecified",
  "claimed_sharpe": <number or null>,
  "claimed_annual_return": <number 0-1 or null>,    // decimal: 0.15 = 15%
  "requires_short_selling": true|false,
  "requires_intraday_data": true|false,
  "requires_options_data": true|false,
  "requires_microstructure_data": true|false,
  "notes": "<anything else relevant — data needs, sample period, caveats>"
}

Title: {title}
Abstract: {abstract}
"""


async def extract_strategy_with_llm(paper: PaperRef) -> Optional[StrategySpec]:
    """Returns None if paper doesn't propose a tradable strategy or LLM unavailable."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return await _extract_anthropic(paper)
    if os.environ.get("OPENAI_API_KEY"):
        return await _extract_openai(paper)
    return None


async def _extract_anthropic(paper: PaperRef) -> Optional[StrategySpec]:
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": _EXTRACT_PROMPT.format(
            title=paper.title, abstract=paper.abstract[:3000],
        )}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post("https://api.anthropic.com/v1/messages",
                              json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
    text = data["content"][0]["text"]
    return _parse_extraction(text)


async def _extract_openai(paper: PaperRef) -> Optional[StrategySpec]:
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Output only valid JSON. No prose."},
            {"role": "user", "content": _EXTRACT_PROMPT.format(
                title=paper.title, abstract=paper.abstract[:3000],
            )},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    return _parse_extraction(text)


def _parse_extraction(text: str) -> Optional[StrategySpec]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        d = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not d.get("tradable"):
        return None
    try:
        return StrategySpec(
            name=str(d.get("name", "Unnamed strategy"))[:80],
            asset_class=StrategyAssetClass(d.get("asset_class", "MULTI")),
            market_type=StrategyMarketType(d.get("market_type", "UNSPECIFIED")),
            signal_summary=str(d.get("signal_summary", "")),
            entry_rule=str(d.get("entry_rule", "")),
            exit_rule=str(d.get("exit_rule", "")),
            holding_period=str(d.get("holding_period", "unspecified")),
            claimed_sharpe=_maybe_float(d.get("claimed_sharpe")),
            claimed_annual_return=_maybe_float(d.get("claimed_annual_return")),
            requires_short_selling=bool(d.get("requires_short_selling", False)),
            requires_intraday_data=bool(d.get("requires_intraday_data", False)),
            requires_options_data=bool(d.get("requires_options_data", False)),
            requires_microstructure_data=bool(d.get("requires_microstructure_data", False)),
            notes=str(d.get("notes", ""))[:500],
        )
    except (ValueError, KeyError) as e:
        log.debug("StrategySpec parse error: %s", e)
        return None


def _maybe_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
