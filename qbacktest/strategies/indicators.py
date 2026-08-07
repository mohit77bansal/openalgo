"""Streaming + vectorized indicators.

Streaming indicators update one bar at a time (state-preserving) — what strategies
use during a backtest. Vectorized variants exist for offline analysis.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@dataclass
class SMA:
    """Simple moving average over the last `period` updates."""
    period: int
    _buf: deque[float] = field(init=False)

    def __post_init__(self) -> None:
        self._buf = deque(maxlen=self.period)

    def update(self, x: float) -> Optional[float]:
        self._buf.append(x)
        if len(self._buf) < self.period:
            return None
        return sum(self._buf) / self.period

    @property
    def value(self) -> Optional[float]:
        if len(self._buf) < self.period:
            return None
        return sum(self._buf) / self.period


@dataclass
class EMA:
    """Exponential moving average. alpha = 2 / (period + 1)."""
    period: int
    alpha: float = 0.0
    _val: Optional[float] = None

    def __post_init__(self) -> None:
        self.alpha = 2.0 / (self.period + 1)

    def update(self, x: float) -> float:
        if self._val is None:
            self._val = x
        else:
            self._val = self.alpha * x + (1 - self.alpha) * self._val
        return self._val


@dataclass
class RSI:
    """Wilder's RSI (smoothed)."""
    period: int = 14
    _gain: float = 0.0
    _loss: float = 0.0
    _prev: Optional[float] = None
    _n: int = 0

    def update(self, x: float) -> Optional[float]:
        if self._prev is None:
            self._prev = x
            return None
        change = x - self._prev
        self._prev = x
        gain = max(change, 0.0)
        loss = -min(change, 0.0)
        self._n += 1
        if self._n <= self.period:
            self._gain += gain
            self._loss += loss
            if self._n < self.period:
                return None
            self._gain /= self.period
            self._loss /= self.period
        else:
            self._gain = (self._gain * (self.period - 1) + gain) / self.period
            self._loss = (self._loss * (self.period - 1) + loss) / self.period
        if self._loss == 0:
            return 100.0
        rs = self._gain / self._loss
        return 100.0 - 100.0 / (1.0 + rs)


@dataclass
class ATR:
    """Wilder's Average True Range."""
    period: int = 14
    _atr: Optional[float] = None
    _prev_close: Optional[float] = None
    _trs: deque[float] = field(default_factory=lambda: deque(maxlen=14))

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self._trs.append(tr)
        if self._atr is None:
            if len(self._trs) < self.period:
                return None
            self._atr = sum(self._trs) / self.period
        else:
            self._atr = (self._atr * (self.period - 1) + tr) / self.period
        return self._atr


@dataclass
class VWAP:
    """Session VWAP — anchored, resets at session open.

    Strategies that use this should call .reset() on session open.
    """
    _cum_pv: float = 0.0
    _cum_v: float = 0.0
    _last_session_date: Optional[date] = None

    def reset(self) -> None:
        self._cum_pv = 0.0
        self._cum_v = 0.0

    def update(self, ts: datetime, typical_price: float, volume: int) -> Optional[float]:
        # Auto-reset across day boundaries
        if self._last_session_date is None or ts.date() != self._last_session_date:
            self.reset()
            self._last_session_date = ts.date()
        self._cum_pv += typical_price * volume
        self._cum_v += volume
        if self._cum_v == 0:
            return None
        return self._cum_pv / self._cum_v


@dataclass
class Bollinger:
    """Bollinger bands. Returns (mid, upper, lower)."""
    period: int = 20
    num_std: float = 2.0
    _buf: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def __post_init__(self) -> None:
        self._buf = deque(maxlen=self.period)

    def update(self, x: float) -> Optional[tuple[float, float, float]]:
        self._buf.append(x)
        if len(self._buf) < self.period:
            return None
        arr = np.array(self._buf)
        mid = float(arr.mean())
        sd = float(arr.std())
        return mid, mid + self.num_std * sd, mid - self.num_std * sd


# ---------------------------------------------------------------------------
# Vectorized
# ---------------------------------------------------------------------------


def rolling_zscore(s: pd.Series, period: int = 20) -> pd.Series:
    rolling = s.rolling(period)
    return (s - rolling.mean()) / rolling.std()


def sma_v(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def ema_v(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def rsi_v(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def atr_v(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()
