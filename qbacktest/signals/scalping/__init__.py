"""Intraday scalping signal engine.

Three detectors that produce honest, time-bounded `TradeSignal`s with explicit
entry, stop, target, R:R, historical win rate, and computed expectancy.

Strategy mix is chosen to give POSITIVE EXPECTANCY rather than chasing high
win rate (the typical retail trap). See docs/RESEARCH.md and
qbacktest.engine.expectancy for why.

Public API:
    TradeSignal, SignalDirection, SignalKind, SignalStatus
    ORBDetector              — Opening Range Breakout (52-55% win, 2:1)
    GapFadeDetector          — Fade overnight gaps >0.5% (62% win, 1.5:1)
    LastHourReversionDetector — Mean-revert overextended last-hour moves (58% win, 1.5:1)
    ScalpingEngine           — runs all detectors against a tick/bar stream
    MockIntradayTicker       — synthetic OHLC bar producer for offline testing
"""

from qbacktest.signals.scalping.types import (
    SignalDirection,
    SignalKind,
    SignalStatus,
    TradeSignal,
)
from qbacktest.signals.scalping.orb import ORBDetector
from qbacktest.signals.scalping.gap_fade import GapFadeDetector
from qbacktest.signals.scalping.reversion import LastHourReversionDetector
from qbacktest.signals.scalping.engine import ScalpingEngine
from qbacktest.signals.scalping.mock_ticker import MockIntradayTicker

__all__ = [
    "GapFadeDetector",
    "LastHourReversionDetector",
    "MockIntradayTicker",
    "ORBDetector",
    "ScalpingEngine",
    "SignalDirection",
    "SignalKind",
    "SignalStatus",
    "TradeSignal",
]
