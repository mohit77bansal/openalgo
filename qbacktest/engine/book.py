"""Position book — tracks all positions, applies fills, computes equity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from qbacktest.core.types import Fill, Instrument, Position


@dataclass
class PositionBook:
    """Mutable container for positions. Mutation happens only inside the engine."""

    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0

    def get_or_create(self, instrument: Instrument) -> Position:
        pos = self.positions.get(instrument.id)
        if pos is None:
            pos = Position(instrument=instrument)
            self.positions[instrument.id] = pos
        return pos

    def apply_fill(self, fill: Fill) -> float:
        """Apply a fill to the book; return realized PnL from this fill.

        Fees are subtracted from realized PnL inside Position.apply_fill.
        """
        pos = self.get_or_create(fill.instrument)
        new_pos, realized_delta = pos.apply_fill(fill)
        self.positions[fill.instrument.id] = new_pos
        self.realized_pnl += realized_delta
        return realized_delta

    def mark_to_market(self, instrument: Instrument, price: float, ts: datetime) -> None:
        pos = self.positions.get(instrument.id)
        if pos is None or pos.is_flat:
            return
        self.positions[instrument.id] = pos.mark(price, ts)

    def total_unrealized(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def total_pnl(self) -> float:
        return self.realized_pnl + self.total_unrealized()

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values() if not p.is_flat)

    def __iter__(self) -> Iterable[Position]:
        return iter(self.positions.values())
