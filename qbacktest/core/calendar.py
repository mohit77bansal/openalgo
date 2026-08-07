"""NSE/BSE trading calendar — sessions, holidays, expiries.

Sources:
- Holidays: NSE holiday list, published annually. We hardcode 2024 and 2025; users
  refresh via `qb seed --year YYYY` which fetches from NSE.
- Weekly expiry days: NSE has changed these multiple times (NIFTY moved Wed→Thu→…).
  We store as a date-keyed override table, with rule-based default.
- Session times: 09:15-15:30 IST for equity & equity F&O, with pre-open 09:00-09:15.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Iterable
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

EQ_SESSION_OPEN = time(9, 15)
EQ_SESSION_CLOSE = time(15, 30)
PRE_OPEN_START = time(9, 0)
PRE_OPEN_END = time(9, 15)


class WeeklyExpiryDay(int, Enum):
    """Day-of-week index (Mon=0)."""
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4


# --- Hardcoded holidays (refresh annually via NSE feed) -----------------------
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
# These are TRADING holidays only (not settlement).
NSE_HOLIDAYS_2024: frozenset[date] = frozenset({
    date(2024, 1, 22),   # Special holiday (Ayodhya)
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 8),    # Mahashivratri
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 11),   # Id-Ul-Fitr
    date(2024, 4, 17),   # Ram Navami
    date(2024, 5, 1),    # Maharashtra Day
    date(2024, 5, 20),   # Lok Sabha Elections (Mumbai)
    date(2024, 6, 17),   # Bakri Id
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Gandhi Jayanti
    date(2024, 11, 1),   # Diwali (Laxmi Pujan — special muhurat session)
    date(2024, 11, 15),  # Guru Nanak Jayanti
    date(2024, 12, 25),  # Christmas
})

NSE_HOLIDAYS_2025: frozenset[date] = frozenset({
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Mahavir Jayanti
    date(2025, 4, 14),   # Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Balipratipada
    date(2025, 11, 5),   # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas
})

NSE_HOLIDAYS_2026: frozenset[date] = frozenset({
    # Placeholder — to be refreshed when NSE publishes 2026 calendar.
    date(2026, 1, 26),
    date(2026, 5, 1),
    date(2026, 8, 15),
    date(2026, 10, 2),
    date(2026, 12, 25),
})

ALL_NSE_HOLIDAYS: frozenset[date] = NSE_HOLIDAYS_2024 | NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026


# --- Weekly expiry rules ------------------------------------------------------
# Each underlying has a current-rule expiry day; historical changes need overrides.
# As of Nov 2024:
#   NIFTY        — Thursday
#   BANKNIFTY    — Wednesday (until Nov 2024 it was Thursday; from Nov 2024 monthly only)
#   FINNIFTY     — Tuesday (until Nov 2024); discontinued weekly Nov 2024
#   MIDCPNIFTY   — Monday; discontinued weekly Nov 2024
#   SENSEX (BSE) — Friday
#
# SEBI rule change (effective Nov 2024): only ONE weekly expiry per exchange.
# After that, NSE has only NIFTY weekly (Thu); BANKNIFTY/FINNIFTY/MIDCPNIFTY
# became monthly-expiry-only.
#
# We encode the LATEST rule and a date-bounded override map.

_DEFAULT_WEEKLY_EXPIRY: dict[str, WeeklyExpiryDay] = {
    "NIFTY": WeeklyExpiryDay.THURSDAY,
    "BANKNIFTY": WeeklyExpiryDay.WEDNESDAY,
    "FINNIFTY": WeeklyExpiryDay.TUESDAY,
    "MIDCPNIFTY": WeeklyExpiryDay.MONDAY,
    "SENSEX": WeeklyExpiryDay.FRIDAY,
    "BANKEX": WeeklyExpiryDay.MONDAY,
}

# (underlying, effective_from_date) → expiry day
# For dates >= effective_from but < the next entry's effective_from, this rule applies.
# Sorted by effective_from ascending.
_HISTORICAL_EXPIRY_OVERRIDES: dict[str, list[tuple[date, WeeklyExpiryDay]]] = {
    "NIFTY": [
        (date(2019, 1, 1), WeeklyExpiryDay.THURSDAY),
    ],
    "BANKNIFTY": [
        (date(2019, 1, 1), WeeklyExpiryDay.THURSDAY),
        (date(2023, 9, 4), WeeklyExpiryDay.WEDNESDAY),
        # Post-Nov 2024 SEBI rule: BANKNIFTY weekly discontinued. Monthly remains
        # last Wednesday.
    ],
    "FINNIFTY": [
        (date(2021, 1, 11), WeeklyExpiryDay.TUESDAY),
    ],
    "MIDCPNIFTY": [
        (date(2024, 7, 8), WeeklyExpiryDay.MONDAY),
    ],
    "SENSEX": [
        (date(2024, 1, 1), WeeklyExpiryDay.FRIDAY),
    ],
}

# After this date, only specified underlyings have weekly expiries.
_WEEKLY_DISCONTINUED_AFTER: dict[str, date] = {
    "BANKNIFTY": date(2024, 11, 13),
    "FINNIFTY": date(2024, 11, 13),
    "MIDCPNIFTY": date(2024, 11, 13),
}


@dataclass(frozen=True)
class TradingCalendar:
    """Pure-functional calendar for NSE/BSE."""

    holidays: frozenset[date] = ALL_NSE_HOLIDAYS

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5  # Sat=5, Sun=6

    def is_trading_day(self, d: date) -> bool:
        return not self.is_weekend(d) and not self.is_holiday(d)

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """Inclusive of both endpoints if they're trading days."""
        out = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    # --- Expiry computation ---

    def weekly_expiry_day(self, underlying: str, on: date) -> WeeklyExpiryDay | None:
        underlying = underlying.upper()
        # SEBI Nov 2024 rule
        cutoff = _WEEKLY_DISCONTINUED_AFTER.get(underlying)
        if cutoff is not None and on > cutoff:
            return None
        history = _HISTORICAL_EXPIRY_OVERRIDES.get(underlying, [])
        active: WeeklyExpiryDay | None = None
        for eff_from, day in history:
            if on >= eff_from:
                active = day
            else:
                break
        if active is None:
            return _DEFAULT_WEEKLY_EXPIRY.get(underlying)
        return active

    def next_weekly_expiry(self, underlying: str, after: date) -> date | None:
        """Next weekly expiry date strictly after `after`. None if discontinued.

        Adjusts forward when the natural expiry day is a holiday: NSE convention
        moves expiry to the previous trading day (T-1), but for weekly options it
        moves forward to the next trading day for the SAME contract series. We
        implement T-1 (the standard for monthly; weekly follows same rule per
        recent NSE circulars).
        """
        target_day = self.weekly_expiry_day(underlying, after)
        if target_day is None:
            return None
        # Find next date with weekday == target_day.value
        cur = after + timedelta(days=1)
        while cur.weekday() != target_day.value:
            cur += timedelta(days=1)
        # Roll back if holiday
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def monthly_expiry(self, underlying: str, year: int, month: int) -> date | None:
        """Last weekday-matching trading day of the month for monthly expiry.

        For most index F&O, monthly expiry = last Thursday (NIFTY) or last Wednesday
        (BANKNIFTY post-Sep-2023). For SENSEX = last Friday.
        """
        target_day = self.weekly_expiry_day(underlying, date(year, month, 28))
        if target_day is None:
            # Even when weekly is discontinued, monthly continues — reuse default.
            target_day = _DEFAULT_WEEKLY_EXPIRY.get(underlying.upper())
            if target_day is None:
                return None
        # Walk from end of month backwards
        if month == 12:
            cur = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            cur = date(year, month + 1, 1) - timedelta(days=1)
        while cur.weekday() != target_day.value or not self.is_trading_day(cur):
            cur -= timedelta(days=1)
            if cur.month != month:
                return None
        return cur

    def all_expiries_in_month(
        self,
        underlying: str,
        year: int,
        month: int,
    ) -> list[date]:
        """All weekly expiries in a given month (includes monthly as the last)."""
        out: list[date] = []
        # Walk from first to last day
        if month == 12:
            last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(year, month + 1, 1) - timedelta(days=1)
        cur_search = date(year, month, 1) - timedelta(days=1)
        while True:
            nxt = self.next_weekly_expiry(underlying, cur_search)
            if nxt is None or nxt > last:
                break
            out.append(nxt)
            cur_search = nxt
        return out

    # --- Session timing ---

    def session_open(self, d: date) -> datetime:
        return datetime.combine(d, EQ_SESSION_OPEN, tzinfo=IST)

    def session_close(self, d: date) -> datetime:
        return datetime.combine(d, EQ_SESSION_CLOSE, tzinfo=IST)

    def is_in_session(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        d = ts.astimezone(IST).date()
        if not self.is_trading_day(d):
            return False
        t = ts.astimezone(IST).time()
        return EQ_SESSION_OPEN <= t <= EQ_SESSION_CLOSE


DEFAULT_CALENDAR = TradingCalendar()
