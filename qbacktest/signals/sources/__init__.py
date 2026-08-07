"""News ingest sources.

Free / low-cost sources that work TODAY without paid API keys:
- moneycontrol_rss : Moneycontrol's RSS feeds (English, no auth)
- economictimes_rss: ET Markets RSS
- livemint_rss     : Mint markets RSS
- gdelt            : GDELT 2.1 free tier (lots of metadata, OK signal)
- nitter           : Nitter mirrors of Twitter — no auth (when alive)
- twitter_v2       : Twitter API v2 if TWITTER_BEARER_TOKEN is set
- bse_announcements: BSE corporate-announcements feed (official)
- nse_announcements: NSE corporate-announcements feed (official)

All sources implement `async def fetch(since: datetime | None) -> list[NewsItem]`.
"""

from qbacktest.signals.sources.rss import RSSSource, MONEYCONTROL_FEEDS, ECONOMICTIMES_FEEDS, MINT_FEEDS
from qbacktest.signals.sources.gdelt import GDELTSource
from qbacktest.signals.sources.twitter import TwitterSource, NitterSource
from qbacktest.signals.sources.exchange_announcements import BSEAnnouncementSource, NSEAnnouncementSource

__all__ = [
    "BSEAnnouncementSource",
    "ECONOMICTIMES_FEEDS",
    "GDELTSource",
    "MINT_FEEDS",
    "MONEYCONTROL_FEEDS",
    "NSEAnnouncementSource",
    "NitterSource",
    "RSSSource",
    "TwitterSource",
]
