"""arxiv.org finance papers source.

Uses arxiv's free Atom API. Categories of interest:
    q-fin.PR - Pricing of Securities
    q-fin.TR - Trading and Microstructure
    q-fin.PM - Portfolio Management
    q-fin.ST - Statistical Finance
    q-fin.MF - Mathematical Finance

No auth, generous rate limit (~1 req/sec).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from xml.etree import ElementTree as ET

import httpx

from qbacktest.research.types import PaperRef

log = logging.getLogger(__name__)


_ARXIV_API = "https://export.arxiv.org/api/query"
_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


async def fetch_arxiv_finance_papers(
    *,
    categories: Iterable[str] = ("q-fin.TR", "q-fin.PR", "q-fin.ST", "q-fin.PM"),
    max_results: int = 100,
    days_back: int = 60,
    keyword_filter: tuple[str, ...] = (
        "arbitrage", "anomaly", "alpha", "predictability", "momentum",
        "mean reversion", "pairs trading", "factor", "trading strategy",
    ),
) -> list[PaperRef]:
    """Fetch recent arxiv finance papers; filter by keywords in title/abstract."""
    cat_q = "+OR+".join(f"cat:{c}" for c in categories)
    params = {
        "search_query": cat_q,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            r = await client.get(_ARXIV_API, params=params)
            if r.status_code != 200:
                log.warning("arxiv returned %s", r.status_code)
                return []
    except httpx.RequestError as e:
        log.warning("arxiv fetch error: %s", e)
        return []

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        log.warning("arxiv parse error: %s", e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    out: list[PaperRef] = []
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        try:
            arxiv_url = entry.findtext("atom:id", default="", namespaces=_NS)
            arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else ""
            title = (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip().replace("\n", " ")
            abstract = (entry.findtext("atom:summary", default="", namespaces=_NS) or "").strip().replace("\n", " ")
            pub_str = entry.findtext("atom:published", default="", namespaces=_NS) or ""
            try:
                published = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published < cutoff:
                continue
            authors = tuple(
                (a.findtext("atom:name", default="", namespaces=_NS) or "").strip()
                for a in entry.findall("atom:author", _NS)
            )
            cats = tuple(
                c.get("term", "")
                for c in entry.findall("atom:category", _NS)
            )
            pdf = ""
            for link in entry.findall("atom:link", _NS):
                if link.get("title") == "pdf":
                    pdf = link.get("href", "")
                    break
            if not pdf and arxiv_id:
                pdf = f"http://arxiv.org/pdf/{arxiv_id}"
            text_blob = (title + " " + abstract).lower()
            if keyword_filter and not any(k in text_blob for k in keyword_filter):
                continue
            out.append(PaperRef(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                abstract=abstract,
                published=published,
                pdf_url=pdf,
                categories=cats,
            ))
        except Exception as e:
            log.debug("arxiv entry skip: %s", e)
            continue
    return out
