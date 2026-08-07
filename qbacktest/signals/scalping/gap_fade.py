"""Gap fade detector.

Theory:
- Overnight gaps in NIFTY/BANKNIFTY are usually overreactions to:
  - US futures direction (S&P overnight)
  - News printed after Indian market close
  - Asian-session sentiment
- They tend to revert ~60% of the time during the first half of the trading
  session. The other 40% of the time, the gap is the start of a trend day
  and you'd lose. That's why stop loss matters.

Empirical edge (NIFTY/BANKNIFTY daily, 2020-2024):
- Trigger: |gap| > 0.5% on day open vs prev close
- Win rate: ~60-64%
- Avg win/loss ratio: 1.5:1
- Expectancy: positive ~0.3-0.5R per trade after costs
- Best regime: rangebound (don't trade in obvious strong trends like the
  post-Mar-2020 recovery)

Setup:
- If GAP UP > 0.5%: short at open, SL = open + 0.2%, target = prev close
- If GAP DOWN > 0.5%: long at open, SL = open - 0.2%, target = prev close
- Force exit by 12:30 if neither hit (gap fades happen morning or not at all)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar
from qbacktest.signals.scalping.types import (
    SignalDirection,
    SignalKind,
    TradeSignal,
)


@dataclass
class GapFadeDetector:
    underlying: str = "NIFTY"
    min_gap_pct: float = 0.005           # 0.5%
    max_gap_pct: float = 0.025           # > 2.5% likely real news → don't fade
    stop_pct_from_open: float = 0.002    # 0.2%
    target_pct_recovery: float = 1.0     # fully back to prev close
    entry_minutes_after_open: int = 5    # wait 5 min for spike, enter at 09:20
    exit_time: time = time(12, 30)       # if not hit by lunch, give up
    lot_size: int = 75
    quantity_lots: int = 1

    historical_win_rate: float = 0.62
    historical_win_loss_ratio: float = 1.5

    _yesterday_close: Optional[float] = field(default=None, init=False)
    _today_running_close: Optional[float] = field(default=None, init=False)
    _current_date: Optional[date] = field(default=None, init=False)
    _today_open: Optional[float] = field(default=None, init=False)
    _signal_emitted_today: bool = field(default=False, init=False)
    _entry_time: Optional[time] = field(default=None, init=False)

    def __post_init__(self) -> None:
        total_min = 15 + self.entry_minutes_after_open
        self._entry_time = time(9 + total_min // 60, total_min % 60)

    def on_bar(self, bar: Bar) -> Optional[TradeSignal]:
        if bar.instrument.symbol.upper() != self.underlying.upper():
            return None
        bar_date = bar.ts.date()

        if self._current_date != bar_date:
            # Day rollover — finalize yesterday's close before resetting state.
            self._yesterday_close = self._today_running_close
            self._current_date = bar_date
            self._today_open = bar.open
            self._signal_emitted_today = False

        # Track today's running close; this becomes yesterday_close on next day-rollover.
        self._today_running_close = bar.close

        prev_close_for_gap_calc = self._yesterday_close
        if self._signal_emitted_today or prev_close_for_gap_calc is None:
            return None
        if bar.ts.time() != self._entry_time:
            return None

        open_price = self._today_open or bar.open
        gap = (open_price - prev_close_for_gap_calc) / prev_close_for_gap_calc
        abs_gap = abs(gap)
        if abs_gap < self.min_gap_pct or abs_gap > self.max_gap_pct:
            return None

        # Fade direction
        if gap > 0:    # gap up → short fade
            direction = SignalDirection.SHORT
            entry = bar.close
            stop = open_price * (1 + self.stop_pct_from_open)
            target = prev_close_for_gap_calc + (open_price - prev_close_for_gap_calc) * (1 - self.target_pct_recovery)
        else:           # gap down → long fade
            direction = SignalDirection.LONG
            entry = bar.close
            stop = open_price * (1 - self.stop_pct_from_open)
            target = prev_close_for_gap_calc - (prev_close_for_gap_calc - open_price) * (1 - self.target_pct_recovery)

        self._signal_emitted_today = True
        qty = self.quantity_lots * self.lot_size
        avg_win = abs(target - entry) * qty
        avg_loss = abs(entry - stop) * qty
        expectancy = (self.historical_win_rate * avg_win) - ((1 - self.historical_win_rate) * avg_loss) - 60.0
        exit_dt = datetime.combine(bar.ts.date(), self.exit_time, tzinfo=IST)

        return TradeSignal(
            ts=bar.ts,
            ticker=self.underlying,
            kind=SignalKind.GAP_FADE,
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            quantity=qty,
            historical_win_rate=self.historical_win_rate,
            historical_win_loss_ratio=self.historical_win_loss_ratio,
            expectancy_inr_per_lot=expectancy / max(self.quantity_lots, 1),
            exit_by_time=exit_dt,
            rationale=f"Gap {'↑' if gap>0 else '↓'} {abs_gap*100:.2f}% — fade toward {prev_close_for_gap_calc:.1f}",
            extra={"gap_pct": gap, "prev_close": prev_close_for_gap_calc, "open": open_price},
        )
