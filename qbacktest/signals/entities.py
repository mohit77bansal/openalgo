"""Entity extraction: map a news headline to NSE-listed tickers.

Approach: simple-but-effective. Each F&O ticker has a set of "match keys"
(the ticker itself + common name forms). We scan headlines for these.

For deeper extraction, a real NER model (spaCy / LLM) gives better recall on
ambiguous mentions ("Bajaj" → BAJAJ-AUTO vs BAJFINANCE). v1 ships heuristic;
LLM-backed version pluggable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Curated F&O ticker → match-keys table.
# Format: ticker_symbol → tuple of regex-safe phrases that should match this stock.
# Order matters: longer/specific first so "TATA STEEL" beats "TATA".
_FNO_MATCH_KEYS: dict[str, tuple[str, ...]] = {
    # Indices
    "NIFTY":      ("NIFTY 50", "NIFTY50", "NIFTY"),
    "BANKNIFTY":  ("BANK NIFTY", "BANKNIFTY", "NIFTY BANK"),
    "FINNIFTY":   ("FINNIFTY", "NIFTY FINANCIAL"),
    "MIDCPNIFTY": ("MIDCPNIFTY", "NIFTY MIDCAP"),
    "SENSEX":     ("SENSEX",),
    # Top stocks (selection — extend from NSE F&O list)
    "RELIANCE":     ("RELIANCE INDUSTRIES", "RELIANCE",),
    "TCS":          ("TATA CONSULTANCY", "TCS",),
    "HDFCBANK":     ("HDFC BANK",),
    "INFY":         ("INFOSYS", "INFY",),
    "ICICIBANK":    ("ICICI BANK",),
    "SBIN":         ("STATE BANK OF INDIA", "SBI", "SBIN",),
    "HINDUNILVR":   ("HINDUSTAN UNILEVER", "HUL",),
    "ITC":          ("ITC LIMITED", "ITC",),
    "LT":           ("LARSEN", "LARSEN & TOUBRO", "L&T",),
    "BHARTIARTL":   ("BHARTI AIRTEL", "AIRTEL",),
    "AXISBANK":     ("AXIS BANK",),
    "KOTAKBANK":    ("KOTAK MAHINDRA BANK", "KOTAK BANK",),
    "ASIANPAINT":   ("ASIAN PAINTS",),
    "MARUTI":       ("MARUTI SUZUKI", "MARUTI",),
    "WIPRO":        ("WIPRO",),
    "ULTRACEMCO":   ("ULTRATECH CEMENT", "ULTRATECH",),
    "TITAN":        ("TITAN COMPANY", "TITAN",),
    "BAJFINANCE":   ("BAJAJ FINANCE",),
    "BAJAJ-AUTO":   ("BAJAJ AUTO",),
    "BAJAJFINSV":   ("BAJAJ FINSERV",),
    "ADANIENT":     ("ADANI ENTERPRISES",),
    "ADANIPORTS":   ("ADANI PORTS",),
    "TATASTEEL":    ("TATA STEEL",),
    "TATAMOTORS":   ("TATA MOTORS",),
    "JSWSTEEL":     ("JSW STEEL",),
    "HINDALCO":     ("HINDALCO",),
    "POWERGRID":    ("POWER GRID", "POWERGRID",),
    "NTPC":         ("NTPC",),
    "ONGC":         ("ONGC", "OIL AND NATURAL GAS",),
    "COALINDIA":    ("COAL INDIA",),
    "DRREDDY":      ("DR REDDY", "DR. REDDY", "DR REDDY'S",),
    "SUNPHARMA":    ("SUN PHARMA",),
    "CIPLA":        ("CIPLA",),
    "NESTLEIND":    ("NESTLE INDIA", "NESTLE",),
    "BRITANNIA":    ("BRITANNIA INDUSTRIES", "BRITANNIA",),
    "DMART":        ("AVENUE SUPERMART", "DMART", "D-MART",),
    "ZOMATO":       ("ZOMATO",),
    "PAYTM":        ("PAYTM", "ONE 97 COMMUNICATIONS",),
    "POLICYBZR":    ("POLICY BAZAAR", "POLICYBAZAAR",),
    "HCLTECH":      ("HCL TECH", "HCL TECHNOLOGIES",),
    "TECHM":        ("TECH MAHINDRA",),
    "M&M":          ("MAHINDRA & MAHINDRA", "M&M",),
    "EICHERMOT":    ("EICHER MOTORS", "ROYAL ENFIELD",),
    "HEROMOTOCO":   ("HERO MOTOCORP",),
    "DIVISLAB":     ("DIVI'S LAB", "DIVIS LAB",),
    "GRASIM":       ("GRASIM",),
    "INDUSINDBK":   ("INDUSIND BANK",),
    "PIDILITIND":   ("PIDILITE",),
    "IOC":          ("INDIAN OIL", "IOC",),
    "BPCL":         ("BPCL", "BHARAT PETROLEUM",),
    "GAIL":         ("GAIL",),
    "VEDL":         ("VEDANTA",),
    "SBILIFE":      ("SBI LIFE",),
    "HDFCLIFE":     ("HDFC LIFE",),
    "ICICIPRULI":   ("ICICI PRUDENTIAL",),
}


# Pre-compiled regex for fast scanning. Word-boundary match, case-insensitive.
_COMPILED: list[tuple[str, re.Pattern]] = []
for ticker, keys in _FNO_MATCH_KEYS.items():
    pattern = "|".join(re.escape(k) for k in keys)
    _COMPILED.append((ticker, re.compile(rf"\b({pattern})\b", re.IGNORECASE)))


# Sectoral keywords → which tickers they affect
_SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BANK_SECTOR":   ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK"),
    "IT_SECTOR":     ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"),
    "AUTO_SECTOR":   ("MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO"),
    "PHARMA_SECTOR": ("SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB"),
    "OIL_GAS":       ("RELIANCE", "ONGC", "IOC", "BPCL", "GAIL"),
    "METALS":        ("TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL"),
}


@dataclass(frozen=True)
class EntityMatch:
    ticker: str
    matched_phrase: str
    span_start: int
    span_end: int


def extract_entities(text: str) -> list[EntityMatch]:
    """Return all NSE F&O tickers mentioned in `text`."""
    matches: list[EntityMatch] = []
    for ticker, pat in _COMPILED:
        for m in pat.finditer(text):
            matches.append(EntityMatch(
                ticker=ticker,
                matched_phrase=m.group(1),
                span_start=m.start(),
                span_end=m.end(),
            ))
    # Deduplicate per ticker (keep first match)
    seen: set[str] = set()
    out: list[EntityMatch] = []
    for m in matches:
        if m.ticker in seen:
            continue
        seen.add(m.ticker)
        out.append(m)
    return out


def expand_to_sectors(tickers: list[str]) -> set[str]:
    """If multiple stocks from the same sector are flagged, also flag the sector."""
    out = set(tickers)
    for sector, members in _SECTOR_KEYWORDS.items():
        if sum(1 for m in members if m in tickers) >= 2:
            out.add(sector)
    return out


def all_known_tickers() -> set[str]:
    return set(_FNO_MATCH_KEYS.keys())
