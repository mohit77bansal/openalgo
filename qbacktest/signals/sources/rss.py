"""RSS news sources — no auth, work today.

Each feed URL is an Atom or RSS 2.0 feed. We parse with stdlib only (no feedparser
dep) to keep the install minimal.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from qbacktest.core.calendar import IST
from qbacktest.signals.types import NewsItem

log = logging.getLogger(__name__)


# Curated free RSS feeds (verified live as of mid-2025)
MONEYCONTROL_FEEDS = {
    "moneycontrol_business":      "https://www.moneycontrol.com/rss/business.xml",
    "moneycontrol_markets":       "https://www.moneycontrol.com/rss/marketreports.xml",
    "moneycontrol_results":       "https://www.moneycontrol.com/rss/results.xml",
    "moneycontrol_buzzingstocks": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
    "moneycontrol_announcements": "https://www.moneycontrol.com/rss/MCtopnews.xml",
}

ECONOMICTIMES_FEEDS = {
    "et_markets":     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "et_stocks":      "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "et_companies":   "https://economictimes.indiatimes.com/news/company/rssfeeds/13352306.cms",
    "et_earnings":    "https://economictimes.indiatimes.com/markets/stocks/earnings/rssfeeds/64829342.cms",
}

MINT_FEEDS = {
    "mint_markets":   "https://www.livemint.com/rss/markets",
    "mint_companies": "https://www.livemint.com/rss/companies",
    "mint_money":     "https://www.livemint.com/rss/money",
}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


@dataclass
class RSSSource:
    """Pull NewsItems from a dict of {source_name: feed_url}."""

    feeds: dict[str, str]
    timeout: float = 12.0

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
            tasks = [self._fetch_one(client, name, url) for name, url in self.feeds.items()]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[NewsItem] = []
        for r in results:
            if isinstance(r, Exception):
                log.warning("RSS fetch error: %s", r)
                continue
            out.extend(r)
        if since is not None:
            out = [n for n in out if n.published_at >= since]
        # Sort newest first
        out.sort(key=lambda n: n.published_at, reverse=True)
        return out

    async def _fetch_one(self, client: httpx.AsyncClient, source_name: str, url: str) -> list[NewsItem]:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                log.warning("%s returned %s", source_name, resp.status_code)
                return []
            return self._parse(source_name, resp.text)
        except httpx.RequestError as e:
            log.warning("%s fetch error: %s", source_name, e)
            return []

    def _parse(self, source_name: str, xml_text: str) -> list[NewsItem]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning("%s parse error: %s", source_name, e)
            return []
        items: list[NewsItem] = []
        # RSS 2.0: //channel/item
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()
            pub_str = (it.findtext("pubDate") or "").strip()
            published_at = _parse_pubdate(pub_str)
            if not title or not link:
                continue
            # Strip HTML tags from description
            body = re.sub(r"<[^>]+>", " ", desc).strip()
            items.append(NewsItem(
                source=source_name,
                title=title,
                url=link,
                published_at=published_at,
                body_excerpt=body[:500],
            ))
        # Atom fallback (entry instead of item)
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title = (it.findtext("atom:title", default="", namespaces=ns) or "").strip()
                link_el = it.find("atom:link", ns)
                link = link_el.get("href", "").strip() if link_el is not None else ""
                summary = (it.findtext("atom:summary", default="", namespaces=ns) or "").strip()
                pub_str = it.findtext("atom:updated", default="", namespaces=ns) or it.findtext(
                    "atom:published", default="", namespaces=ns) or ""
                published_at = _parse_pubdate(pub_str)
                if not title or not link:
                    continue
                items.append(NewsItem(
                    source=source_name,
                    title=title,
                    url=link,
                    published_at=published_at,
                    body_excerpt=re.sub(r"<[^>]+>", " ", summary).strip()[:500],
                ))
        return items


def _parse_pubdate(s: str) -> datetime:
    """Parse RFC 822, RFC 3339, or ISO 8601 dates. Fallback to now()."""
    s = s.strip()
    if not s:
        return datetime.now(IST)
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:
        # ISO 8601 (Atom)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(IST)
