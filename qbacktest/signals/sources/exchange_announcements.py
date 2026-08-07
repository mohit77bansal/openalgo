"""Official corporate-announcement feeds from BSE / NSE.

These are the highest-quality news sources for "what's about to move":
- Earnings results
- M&A
- Regulatory orders
- Block deals
- Pledge / encumbrance changes

BSE provides a JSON API (no auth, public).
NSE has a more protected feed (Akamai); we keep a stub for tomorrow when
authenticated broker data is available.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

from qbacktest.core.calendar import IST
from qbacktest.signals.types import NewsItem

log = logging.getLogger(__name__)


_BSE_ANNOUNCEMENT_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


@dataclass
class BSEAnnouncementSource:
    """Fetch latest BSE corporate announcements (no auth).

    BSE's public API gives us scrip code, headline, date, attachment URL.
    """

    timeout: float = 12.0
    look_back_days: int = 1
    category: str = "-1"   # all categories

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        end = datetime.now(IST).date()
        start = end - timedelta(days=self.look_back_days)
        params = {
            "pageno": 1,
            "strCat": self.category,
            "strPrevDate": start.strftime("%Y%m%d"),
            "strScrip": "",
            "strSearch": "P",
            "strToDate": end.strftime("%Y%m%d"),
            "strType": "C",
            "subcategory": "-1",
        }
        async with httpx.AsyncClient(headers=_BSE_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
            try:
                resp = await client.get(_BSE_ANNOUNCEMENT_URL, params=params)
                if resp.status_code != 200:
                    log.warning("BSE announcements returned %s", resp.status_code)
                    return []
                data = resp.json()
            except (httpx.RequestError, ValueError) as e:
                log.warning("BSE fetch error: %s", e)
                return []
        rows = data.get("Table", []) or []
        out: list[NewsItem] = []
        for r in rows:
            try:
                pub_str = (r.get("DT_TM") or "").strip()
                published_at = _bse_parse_date(pub_str)
                if since is not None and published_at < since:
                    continue
                title_parts = [
                    r.get("SLONGNAME", "").strip(),    # company name
                    r.get("HEADLINE", "").strip(),     # announcement headline
                ]
                title = " — ".join(p for p in title_parts if p) or "BSE announcement"
                # Build URL (BSE attachment server)
                attach = (r.get("ATTACHMENTNAME") or "").strip()
                url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}" if attach else "https://www.bseindia.com/corporates/ann.html"
                out.append(NewsItem(
                    source="bse_announcements",
                    title=title,
                    url=url,
                    published_at=published_at,
                    body_excerpt=(r.get("MORE") or "").strip()[:500],
                    raw=r,
                ))
            except Exception as e:
                log.debug("BSE row skip: %s", e)
                continue
        out.sort(key=lambda n: n.published_at, reverse=True)
        return out


@dataclass
class NSEAnnouncementSource:
    """Stub — NSE's announcement feed is Akamai-protected.

    Full implementation requires either:
    - Authenticated broker access (Upstox/AngelOne), OR
    - A real-browser navigator (Playwright) with stealth tweaks

    Returns [] for now. Will be wired tomorrow alongside the broker chain ingest.
    """

    async def fetch(self, since: Optional[datetime] = None) -> list[NewsItem]:
        return []


def _bse_parse_date(s: str) -> datetime:
    if not s:
        return datetime.now(IST)
    # BSE format: "10 May 2026 22:34:11"
    for fmt in ("%d %b %Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return datetime.now(IST)
