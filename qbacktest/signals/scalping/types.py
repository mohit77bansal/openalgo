"""Domain types for scalping signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalKind(str, Enum):
    ORB_BREAKOUT = "ORB_BREAKOUT"
    GAP_FADE = "GAP_FADE"
    LAST_HOUR_REVERSION = "LAST_HOUR_REVERSION"
    EOD_SQUARE_OFF = "EOD_SQUARE_OFF"


class SignalStatus(str, Enum):
    PROPOSED = "PROPOSED"        # detector emitted; not yet executed
    ENTERED = "ENTERED"          # trader took the trade
    HIT_TARGET = "HIT_TARGET"
    HIT_STOP = "HIT_STOP"
    TIME_EXIT = "TIME_EXIT"      # forced flat at exit_by_time
    SKIPPED = "SKIPPED"          # regime filter blocked, or manual skip


@dataclass(frozen=True, slots=True)
class TradeSignal:
    """One actionable intraday trade idea — entry, stop, target, expectancy.

    Designed for retail manual execution: the trader sees this card, enters
    three orders (entry market + SL + target as separate brackets), and walks
    away.
    """

    ts: datetime
    ticker: str                  # NIFTY, BANKNIFTY, RELIANCE etc.
    kind: SignalKind
    direction: SignalDirection
    entry: float
    stop: float
    target: float
    quantity: int                # suggested qty in units (lots × lot_size)

    # Honest math (NOT just win rate)
    historical_win_rate: float           # 0..1, from prior research
    historical_win_loss_ratio: float     # avg_win / |avg_loss|
    expectancy_inr_per_lot: float        # E[trade] in INR, after costs

    # Decay
    exit_by_time: datetime               # force-flat at this time (typically 15:25 IST)
    status: SignalStatus = SignalStatus.PROPOSED
    rationale: str = ""

    # Optional regime adjustments
    size_multiplier: float = 1.0
    regime_note: Optional[str] = None

    extra: dict[str, object] = field(default_factory=dict, hash=False)

    @property
    def risk_inr_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_inr_per_unit(self) -> float:
        return abs(self.target - self.entry)

    @property
    def reward_to_risk(self) -> float:
        risk = self.risk_inr_per_unit
        return self.reward_inr_per_unit / risk if risk > 0 else 0.0

    @property
    def breakeven_win_rate(self) -> float:
        rr = self.reward_to_risk
        return 1.0 / (1.0 + rr) if rr > 0 else 1.0

    @property
    def edge_vs_breakeven(self) -> float:
        return self.historical_win_rate - self.breakeven_win_rate

    @property
    def total_risk_inr(self) -> float:
        return self.risk_inr_per_unit * self.quantity

    @property
    def total_reward_inr(self) -> float:
        return self.reward_inr_per_unit * self.quantity

    def to_json(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "ticker": self.ticker,
            "kind": self.kind.value,
            "direction": self.direction.value,
            "entry": round(self.entry, 2),
            "stop": round(self.stop, 2),
            "target": round(self.target, 2),
            "quantity": self.quantity,
            "risk_inr_per_unit": round(self.risk_inr_per_unit, 2),
            "reward_inr_per_unit": round(self.reward_inr_per_unit, 2),
            "reward_to_risk": round(self.reward_to_risk, 3),
            "total_risk_inr": round(self.total_risk_inr, 2),
            "total_reward_inr": round(self.total_reward_inr, 2),
            "historical_win_rate": round(self.historical_win_rate, 3),
            "historical_win_loss_ratio": round(self.historical_win_loss_ratio, 3),
            "breakeven_win_rate": round(self.breakeven_win_rate, 3),
            "edge_vs_breakeven": round(self.edge_vs_breakeven, 3),
            "expectancy_inr_per_lot": round(self.expectancy_inr_per_lot, 2),
            "exit_by_time": self.exit_by_time.isoformat(),
            "status": self.status.value,
            "rationale": self.rationale,
            "size_multiplier": round(self.size_multiplier, 3),
            "regime_note": self.regime_note,
        }
