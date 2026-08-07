"""MockIntradayTicker — synthetic NIFTY 1-minute bars for offline scalping demos.

Generates realistic NIFTY-style price action:
- ~9:15 IST open with a stochastic overnight gap vs prev close
- Random walk with mild trend bias and intraday vol
- Occasional injected regimes (trending day, range day, late-day reversal)
  to stress-test the detectors

Used by `qb scan-scalping --source mock` and the gateway's in-process worker.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar, Exchange, Instrument, InstrumentType


@dataclass
class MockIntradayTicker:
    underlying: str = "NIFTY"
    start_price: float = 25_000.0
    interval_seconds: float = 1.0      # wall-clock seconds between bars
    minute_step: int = 1               # in-bar timestamp step (minutes)
    daily_vol_pct: float = 0.0035      # ~0.35% per-minute σ (calibrated to NIFTY 1m realised vol)
    overnight_gap_max_pct: float = 0.015
    inject_regime: str = "random"      # random / trend_up / trend_down / range / late_reversal

    _now: Optional[datetime] = field(default=None, init=False)
    _price: Optional[float] = field(default=None, init=False)
    _day_open: Optional[float] = field(default=None, init=False)
    _rng: random.Random = field(default_factory=random.Random, init=False)
    _last_emit_wall: Optional[datetime] = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    _instrument: Instrument = field(init=False)
    _bar_idx: int = field(default=0, init=False)
    _today_regime: str = field(default="range", init=False)

    def __post_init__(self) -> None:
        self._instrument = Instrument(
            symbol=self.underlying.upper(),
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.INDEX,
            lot_size=1,
        )
        # Start clock at most-recent session open
        now = datetime.now(IST)
        if now.time() < time(9, 15):
            session_open = datetime.combine(now.date(), time(9, 15), tzinfo=IST)
        else:
            session_open = datetime.combine(now.date(), time(9, 15), tzinfo=IST)
        self._now = session_open
        self._price = self.start_price
        self._day_open = self.start_price
        self._set_today_regime()

    def _set_today_regime(self) -> None:
        if self.inject_regime != "random":
            self._today_regime = self.inject_regime
        else:
            self._today_regime = self._rng.choice(
                ["trend_up", "trend_down", "range", "range", "late_reversal", "gap_fade"]
            )

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def next(self) -> Bar:
        # Pace
        if self._last_emit_wall is not None:
            elapsed = (datetime.now(IST) - self._last_emit_wall).total_seconds()
            if elapsed < self.interval_seconds:
                await asyncio.sleep(self.interval_seconds - elapsed)
        bar = self._build_bar()
        self._last_emit_wall = datetime.now(IST)
        return bar

    def _build_bar(self) -> Bar:
        assert self._now is not None and self._price is not None and self._day_open is not None
        # Roll the day at 15:30
        if self._now.time() >= time(15, 30):
            # Overnight gap
            gap = self._rng.gauss(0, self.overnight_gap_max_pct / 2)
            gap = max(-self.overnight_gap_max_pct, min(self.overnight_gap_max_pct, gap))
            self._price *= (1 + gap)
            self._day_open = self._price
            # Next session open
            next_day = self._now.date() + timedelta(days=1)
            # Skip weekends
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            self._now = datetime.combine(next_day, time(9, 15), tzinfo=IST)
            self._set_today_regime()
            self._bar_idx = 0

        # Regime-tuned step
        drift, vol_scale = self._regime_step()
        z = self._rng.gauss(0, 1)
        sigma = self.daily_vol_pct * vol_scale
        change = drift + sigma * z
        open_px = self._price
        close_px = self._price * (1 + change)
        # Synthetic high/low spread
        spread = abs(z) * sigma * self._price * 0.5
        high_px = max(open_px, close_px) + spread
        low_px = min(open_px, close_px) - spread

        bar = Bar(
            instrument=self._instrument,
            ts=self._now,
            timeframe="1m",
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            volume=self._rng.randint(50_000, 800_000),
        )
        self._price = close_px
        self._now = self._now + timedelta(minutes=self.minute_step)
        self._bar_idx += 1
        return bar

    def _regime_step(self) -> tuple[float, float]:
        """Return (drift_per_minute, vol_scale) for the active intraday regime."""
        assert self._now is not None
        t = self._now.time()
        morning = t < time(11, 0)
        late = t >= time(14, 0)
        if self._today_regime == "trend_up":
            return (0.00015 if morning else 0.0001, 1.0)
        if self._today_regime == "trend_down":
            return (-0.00015 if morning else -0.0001, 1.0)
        if self._today_regime == "range":
            return (0.0, 0.7)
        if self._today_regime == "late_reversal":
            # Trend up morning, fade afternoon → triggers reversion detector
            if morning:
                return (0.00020, 1.1)
            if late:
                return (-0.00018, 1.3)
            return (0.00010, 1.0)
        if self._today_regime == "gap_fade":
            # Strong opening overshoot then fade back
            if t < time(9, 25):
                return (0.0008, 2.0)
            return (-0.00010, 0.9)
        return (0.0, 1.0)
