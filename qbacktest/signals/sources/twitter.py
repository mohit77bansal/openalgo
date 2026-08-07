"""Twitter sources.

Two paths:
- TwitterSource: Twitter API v2 — requires TWITTER_BEARER_TOKEN. Paid tier
  needed for search/recent at any meaningful volume (Basic = $100/mo as of 2024).
- NitterSource: Nitter mirrors of Twitter — public, no auth, but mirrors are
  often blocked or rate-limited. Best-effort.

For Indian markets, a curated handle list works better than keyword search.
Defaults are commentary-heavy accounts (BloombergQuint, ETMarkets, financial
analysts) — adjust to taste.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from qbacktest.core.calendar import IST
from qbacktest.signals.types import NewsItem

log = logging.getLogger(__name__)


_DEFAULT_HANDLES = (
    "bsindia",
    "ETMarkets",
    "moneycontrolcom",
    "livemint",
    "FinancialXpress",
    "ndtvprofit",
    "Reuters_co_in",
)


@dataclass
class TwitterSource:
    """Twitter API v2 client. Requires bearer token in env."""

    handles: tuple[str, ...] = _DEFAULT_HANDLES
    bearer_token: Optional[str] = None
    timeout: float = 12.0
    max_per_handle: int = 20

    def __post_init__(self) -> None:
        if self.bearer_token is None:
            self.bearer_token = os.environ.get("TWITTER_BEARER_TOKEN", "")

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        if not self.bearer_token:
            log.info("TWITTER_BEARER_TOKEN not set — TwitterSource skipped")
            return []
        out: list[NewsItem] = []
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.bearer_token}"},
            timeout=self.timeout,
        ) as client:
            for handle in self.handles:
                try:
                    out.extend(await self._fetch_handle(client, handle))
                except Exception as e:
                    log.warning("twitter %s fetch error: %s", handle, e)
        if since is not None:
            out = [n for n in out if n.published_at >= since]
        out.sort(key=lambda n: n.published_at, reverse=True)
        return out

    async def _fetch_handle(self, client: httpx.AsyncClient, handle: str) -> list[NewsItem]:
        # Step 1: resolve username → user_id
        u = await client.get(f"https://api.twitter.com/2/users/by/username/{handle}")
        if u.status_code != 200:
            log.warning("twitter user lookup %s -> %s", handle, u.status_code)
            return []
        user_id = u.json().get("data", {}).get("id")
        if not user_id:
            return []
        # Step 2: latest tweets
        t = await client.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            params={
                "max_results": str(self.max_per_handle),
                "tweet.fields": "created_at,public_metrics,context_annotations",
            },
        )
        if t.status_code != 200:
            log.warning("twitter tweets %s -> %s", handle, t.status_code)
            return []
        tweets = t.json().get("data", []) or []
        out = []
        for tw in tweets:
            ts_raw = tw.get("created_at")
            if ts_raw:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(IST)
            else:
                ts = datetime.now(IST)
            text = (tw.get("text") or "").strip()
            if not text:
                continue
            out.append(NewsItem(
                source=f"twitter:{handle}",
                title=text[:280],
                url=f"https://twitter.com/{handle}/status/{tw['id']}",
                published_at=ts,
                body_excerpt=text,
                raw=tw,
            ))
        return out


@dataclass
class NitterSource:
    """Best-effort Nitter scraper. Tries multiple instances; many are flaky."""

    handles: tuple[str, ...] = _DEFAULT_HANDLES
    timeout: float = 10.0
    instances: tuple[str, ...] = (
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    )

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        out: list[NewsItem] = []
        # Pick the first responsive instance
        instance = await self._pick_instance()
        if instance is None:
            log.info("No nitter instance reachable — NitterSource returning empty")
            return []
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for handle in self.handles:
                try:
                    out.extend(await self._fetch_one(client, instance, handle))
                except Exception as e:
                    log.warning("nitter %s/%s: %s", instance, handle, e)
        if since is not None:
            out = [n for n in out if n.published_at >= since]
        out.sort(key=lambda n: n.published_at, reverse=True)
        return out

    async def _pick_instance(self) -> Optional[str]:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            for inst in self.instances:
                try:
                    r = await client.get(inst)
                    if r.status_code == 200 and "Nitter" in r.text:
                        return inst
                except httpx.RequestError:
                    continue
        return None

    async def _fetch_one(self, client: httpx.AsyncClient, instance: str, handle: str) -> list[NewsItem]:
        # Use the RSS endpoint that Nitter exposes
        url = f"{instance}/{handle}/rss"
        r = await client.get(url)
        if r.status_code != 200:
            return []
        # Reuse RSS parser
        from qbacktest.signals.sources.rss import RSSSource, _parse_pubdate
        items: list[NewsItem] = []
        from xml.etree import ElementTree as ET
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            return []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = _parse_pubdate(it.findtext("pubDate") or "")
            items.append(NewsItem(
                source=f"nitter:{handle}",
                title=title,
                url=link,
                published_at=pub,
                body_excerpt=(it.findtext("description") or "").strip()[:500],
            ))
        return items
