"""GDELT 2.1 free tier — global news with NER tags.

GDELT's free GDELT Doc 2.0 API supports filtered queries by company, country,
language. Useful for second-source corroboration of Indian news.

API: https://api.gdeltproject.org/api/v2/doc/doc?query=...&format=json
No auth, generous rate limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from qbacktest.core.calendar import IST
from qbacktest.signals.types import NewsItem

log = logging.getLogger(__name__)


_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclass
class GDELTSource:
    """Fetch news mentioning Indian companies / markets in the last N hours."""

    timeout: float = 12.0
    max_records: int = 100
    countries: tuple[str, ...] = ("IN",)
    keywords: tuple[str, ...] = (
        "NSE", "BSE", "Nifty", "Sensex", "Reliance", "TCS", "HDFC", "Infosys",
        "RBI", "SEBI", "Indian stocks", "Indian markets",
    )
    timespan: str = "24H"

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        # Build a query: any of our keywords AND sourcecountry:IN
        kw = " OR ".join(f'"{k}"' for k in self.keywords)
        country_filter = " OR ".join(f"sourcecountry:{c}" for c in self.countries)
        query = f"({kw}) AND ({country_filter})"
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(min(self.max_records, 250)),
            "sort": "datedesc",
            "timespan": self.timespan,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(_GDELT_URL, params=params)
                if resp.status_code != 200:
                    log.warning("GDELT returned %s", resp.status_code)
                    return []
                data = resp.json()
        except (httpx.RequestError, ValueError) as e:
            log.warning("GDELT fetch error: %s", e)
            return []
        articles = data.get("articles", []) or []
        out: list[NewsItem] = []
        for a in articles:
            try:
                # GDELT timestamps are UTC like "20260510T223500Z"
                ts_raw = a.get("seendate", "")
                if ts_raw:
                    ts = datetime.strptime(ts_raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(IST)
                else:
                    ts = datetime.now(IST)
                if since is not None and ts < since:
                    continue
                out.append(NewsItem(
                    source="gdelt",
                    title=(a.get("title") or "").strip(),
                    url=(a.get("url") or "").strip(),
                    published_at=ts,
                    body_excerpt="",
                    raw=a,
                ))
            except Exception as e:
                log.debug("GDELT row skip: %s", e)
                continue
        return out
