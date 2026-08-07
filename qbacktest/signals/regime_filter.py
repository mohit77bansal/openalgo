"""Regime / event filter.

Blocks premium-selling strategies on event days. Returns a structured
`RegimeDecision` so strategies can opt in via:

    decision = regime.check(date.today(), kind=StrategyKind.PREMIUM_SELL)
    if not decision.allowed:
        ctx.log("trade_skipped_regime", reason=decision.reason)
        return

Sources for the event calendar:
- Hardcoded RBI policy dates 2025–2026 (refresh from rbi.org.in/bimonthly schedule)
- Union budget date (typically Feb 1)
- F&O expiry days (computed from TradingCalendar)
- Top-20 stock earnings windows (configurable, with bellwether IT companies pre-loaded)

A real production system would pull these from a calendar API (or BSE corporate
announcements feed); we hardcode the high-cardinality ones to ship working code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional

from qbacktest.core.calendar import DEFAULT_CALENDAR


class StrategyKind(str, Enum):
    """Categories the filter cares about."""
    PREMIUM_SELL = "PREMIUM_SELL"          # short straddle, short strangle, short condor
    PREMIUM_BUY = "PREMIUM_BUY"             # long straddle, long calls, long puts
    DIRECTIONAL = "DIRECTIONAL"             # ORB, momentum, breakout
    MEAN_REVERSION = "MEAN_REVERSION"
    ARBITRAGE = "ARBITRAGE"                 # box, parity — always allowed
    SCALPING = "SCALPING"                   # any intraday quick-in-quick-out


class EventKind(str, Enum):
    RBI_POLICY = "RBI_POLICY"
    BUDGET = "BUDGET"
    FNO_EXPIRY = "FNO_EXPIRY"
    BELLWETHER_RESULT = "BELLWETHER_RESULT"
    FED_FOMC = "FED_FOMC"                   # spills over to Indian markets next day
    ELECTION_RESULT = "ELECTION_RESULT"


# --- Hardcoded event calendar -------------------------------------------- #

# RBI Monetary Policy Committee dates — refresh from https://rbi.org.in
# Pattern: bi-monthly (6/year). Verified through 2026.
RBI_POLICY_DATES: frozenset[date] = frozenset({
    # 2025
    date(2025, 2, 7),
    date(2025, 4, 9),
    date(2025, 6, 6),
    date(2025, 8, 6),
    date(2025, 10, 1),
    date(2025, 12, 5),
    # 2026
    date(2026, 2, 6),
    date(2026, 4, 8),
    date(2026, 6, 5),
    date(2026, 8, 5),
    date(2026, 10, 7),
    date(2026, 12, 4),
})

UNION_BUDGET_DATES: frozenset[date] = frozenset({
    date(2025, 2, 1),
    date(2026, 2, 1),
})

# FOMC dates — refresh from federalreserve.gov calendar
FED_FOMC_DATES: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 5, 6),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 11, 4),
    date(2026, 12, 16),
})

# Bellwether stocks whose results day = sector-wide vol spike
# (typically Q4 result dates fall mid-April; Q1 mid-July; Q2 mid-Oct; Q3 mid-Jan)
# Refresh from BSE results calendar
BELLWETHER_RESULTS_WINDOW: dict[str, list[tuple[date, date]]] = {
    # (start_date, end_date) — inclusive. Sector affected via lookup
    "TCS":      [(date(2026, 4, 9),  date(2026, 4, 11)),
                 (date(2026, 7, 10), date(2026, 7, 12)),
                 (date(2026, 10, 9), date(2026, 10, 11))],
    "INFY":     [(date(2026, 4, 11), date(2026, 4, 13)),
                 (date(2026, 7, 16), date(2026, 7, 18)),
                 (date(2026, 10, 15), date(2026, 10, 17))],
    "RELIANCE": [(date(2026, 4, 19), date(2026, 4, 21))],
    "HDFCBANK": [(date(2026, 4, 17), date(2026, 4, 19))],
}


# --- Decision type -------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    allowed: bool
    reason: str
    event_kind: Optional[EventKind] = None
    severity: str = "info"        # "info" | "warn" | "block"
    affected_tickers: tuple[str, ...] = ()
    suggested_size_multiplier: float = 1.0   # 0.5 = trade half size; 0 = skip

    def __bool__(self) -> bool:   # truthy = allowed
        return self.allowed


# --- Filter --------------------------------------------------------------- #


@dataclass
class RegimeFilter:
    """Composable rule set. Each rule returns Optional[RegimeDecision]; first
    blocking decision wins. Information-only rules adjust size_multiplier.
    """

    block_premium_sell_on_rbi: bool = True
    block_premium_sell_on_budget: bool = True
    block_premium_sell_on_fomc_followthrough: bool = True
    block_premium_sell_on_results_week: bool = True
    block_directional_on_expiry_close: bool = False   # ORB/breakout can run on expiry; just size down
    expiry_day_size_multiplier: float = 0.5
    results_window_size_multiplier: float = 0.3

    def check(
        self,
        on: date,
        *,
        kind: StrategyKind,
        ticker: Optional[str] = None,
    ) -> RegimeDecision:
        # Arbitrage is always allowed — pure-arb has no event risk if held to expiry
        if kind == StrategyKind.ARBITRAGE:
            return RegimeDecision(allowed=True, reason="Arbitrage allowed in all regimes")

        # RBI policy day → block premium-selling
        if on in RBI_POLICY_DATES:
            if kind == StrategyKind.PREMIUM_SELL and self.block_premium_sell_on_rbi:
                return RegimeDecision(
                    allowed=False,
                    reason=f"RBI Monetary Policy on {on} — premium selling blocked (gap risk)",
                    event_kind=EventKind.RBI_POLICY,
                    severity="block",
                )
            if kind == StrategyKind.DIRECTIONAL:
                return RegimeDecision(
                    allowed=True,
                    reason=f"RBI Policy on {on} — directional OK, reduce size",
                    event_kind=EventKind.RBI_POLICY,
                    severity="warn",
                    suggested_size_multiplier=0.5,
                )

        # Budget day → block premium-selling for the day AND the following session
        budget_window = UNION_BUDGET_DATES | {d + timedelta(days=1) for d in UNION_BUDGET_DATES}
        if on in budget_window:
            if kind == StrategyKind.PREMIUM_SELL and self.block_premium_sell_on_budget:
                return RegimeDecision(
                    allowed=False,
                    reason=f"Union Budget window {on} — premium selling blocked (sector tail risk)",
                    event_kind=EventKind.BUDGET,
                    severity="block",
                )

        # FOMC happens overnight US → spills over to Indian markets the next trading day
        if self.block_premium_sell_on_fomc_followthrough and kind == StrategyKind.PREMIUM_SELL:
            for fed_date in FED_FOMC_DATES:
                # Indian market reaction day = next trading day after FOMC announcement
                # FOMC typically announces around midnight IST → reaction is same calendar date or +1
                next_trading = DEFAULT_CALENDAR.next_trading_day(fed_date - timedelta(days=1))
                if on == next_trading:
                    return RegimeDecision(
                        allowed=False,
                        reason=f"FOMC follow-through ({fed_date}) — premium selling blocked",
                        event_kind=EventKind.FED_FOMC,
                        severity="block",
                    )

        # Bellwether results week
        if self.block_premium_sell_on_results_week and ticker:
            for stock, windows in BELLWETHER_RESULTS_WINDOW.items():
                for start, end in windows:
                    if start <= on <= end:
                        if ticker == stock and kind == StrategyKind.PREMIUM_SELL:
                            return RegimeDecision(
                                allowed=False,
                                reason=f"{stock} results window {start}–{end} — single-name premium-sell blocked",
                                event_kind=EventKind.BELLWETHER_RESULT,
                                severity="block",
                                affected_tickers=(stock,),
                            )
                        # Sector-bellwether: TCS/INFY results spike sector vol
                        if stock in ("TCS", "INFY") and ticker in ("WIPRO", "HCLTECH", "TECHM"):
                            return RegimeDecision(
                                allowed=True,
                                reason=f"{stock} results window — IT sector vol elevated; reduce size",
                                event_kind=EventKind.BELLWETHER_RESULT,
                                severity="warn",
                                affected_tickers=(stock, ticker),
                                suggested_size_multiplier=self.results_window_size_multiplier,
                            )

        # Expiry day — reduce directional size, don't block
        underlying = ticker or "NIFTY"
        if kind in (StrategyKind.DIRECTIONAL, StrategyKind.SCALPING):
            try:
                nxt_expiry = DEFAULT_CALENDAR.next_weekly_expiry(underlying, on - timedelta(days=1))
                if nxt_expiry == on:
                    return RegimeDecision(
                        allowed=True,
                        reason=f"{underlying} weekly expiry — pin risk, reduce size",
                        event_kind=EventKind.FNO_EXPIRY,
                        severity="warn",
                        suggested_size_multiplier=self.expiry_day_size_multiplier,
                    )
            except (ValueError, KeyError):
                pass

        # All clear
        return RegimeDecision(allowed=True, reason="Normal regime")


DEFAULT_REGIME_FILTER = RegimeFilter()
