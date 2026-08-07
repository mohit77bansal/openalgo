"""Chain source abstraction.

A ChainSource yields ChainSnapshot objects on demand. The scanner worker
calls `await source.next()` in a loop. Concrete implementations:

  - LiveBrokerChainSource: wraps ChainBuilder over a real BrokerAdapter
  - MockChainProducer:     synthesizes BS-priced chains with stochastic
                           mispricings — for offline demos and tests

This is the seam that lets us run the full pipeline offline today and flip to
real broker data with a single line change.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Protocol

from qbacktest.core.calendar import IST
from qbacktest.core.chain import ChainQuote, ChainSnapshot
from qbacktest.core.lot_sizes import lot_size_on
from qbacktest.core.types import OptionType
from qbacktest.greeks import bs_price


class ChainSource(Protocol):
    """Async iterator-style source of chain snapshots."""

    async def next(self) -> ChainSnapshot:
        """Block until the next snapshot is available."""

    async def start(self) -> None:
        """Begin producing (subscribe to broker, start mock loop, etc)."""

    async def stop(self) -> None:
        """Stop producing."""


@dataclass
class MockChainProducer:
    """Synthesize a NIFTY chain on a fixed cadence.

    On each tick:
      - Spot performs a small random walk (geometric Brownian, σ=0.5% per tick)
      - All options are re-priced via Black-Scholes
      - With probability `mispricing_prob`, ONE strike's quote is perturbed to
        create a real arbitrage opportunity (configurable kind)

    This is useful for:
      - Demoing the full live pipeline without broker creds
      - End-to-end tests of the scanner worker + WebSocket
      - Reproducible scenarios for development
    """

    underlying: str = "NIFTY"
    initial_spot: float = 25_000.0
    iv: float = 0.18
    rfr: float = 0.07
    days_to_expiry: int = 14
    strike_step: int = 50
    n_strikes_each_side: int = 10
    interval_seconds: float = 1.0
    mispricing_prob: float = 0.30
    mispricing_kinds: tuple[str, ...] = ("box", "adjacent", "parity")
    seed: Optional[int] = None

    _spot: float = field(init=False)
    _expiry: date = field(init=False)
    _tick_count: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    _last_yield_ts: Optional[datetime] = field(default=None, init=False)
    _rng: random.Random = field(init=False)
    _lot_size: int = field(init=False)

    def __post_init__(self) -> None:
        self._spot = self.initial_spot
        self._expiry = date.today() + timedelta(days=self.days_to_expiry)
        self._rng = random.Random(self.seed)
        self._lot_size = lot_size_on(self.underlying, date.today())

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def next(self) -> ChainSnapshot:
        if not self._started:
            await self.start()
        # Pace the loop
        if self._last_yield_ts is not None:
            elapsed = (datetime.now(IST) - self._last_yield_ts).total_seconds()
            remaining = self.interval_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        snap = self._build_snapshot()
        self._last_yield_ts = datetime.now(IST)
        self._tick_count += 1
        return snap

    # --- Internal --------------------------------------------------------- #

    def _build_snapshot(self) -> ChainSnapshot:
        # Random walk on spot
        drift = 0.0
        sigma_per_tick = 0.005   # 0.5% per tick
        z = self._rng.gauss(0, 1)
        self._spot *= math.exp(drift - 0.5 * sigma_per_tick ** 2 + sigma_per_tick * z)

        atm = round(self._spot / self.strike_step) * self.strike_step
        strikes = [atm + (i - self.n_strikes_each_side) * self.strike_step
                   for i in range(2 * self.n_strikes_each_side + 1)]

        days_left = max((self._expiry - date.today()).days, 1)
        t = days_left / 365.0
        bid_offset = 0.05

        quotes: list[ChainQuote] = []
        for k in strikes:
            c_px = bs_price(self._spot, k, t, self.rfr, self.iv, "CE")
            p_px = bs_price(self._spot, k, t, self.rfr, self.iv, "PE")
            # Spread proportional to distance from ATM (illiquid wings)
            dist = abs(k - self._spot) / self._spot
            spread = bid_offset + dist * 5.0
            quotes.append(ChainQuote(
                strike=float(k), option_type=OptionType.CALL,
                last_price=c_px, bid=max(c_px - spread, 0.05), ask=c_px + spread,
                iv=self.iv, volume=self._rng.randint(100, 100_000),
                open_interest=self._rng.randint(1_000, 5_000_000),
            ))
            quotes.append(ChainQuote(
                strike=float(k), option_type=OptionType.PUT,
                last_price=p_px, bid=max(p_px - spread, 0.05), ask=p_px + spread,
                iv=self.iv, volume=self._rng.randint(100, 100_000),
                open_interest=self._rng.randint(1_000, 5_000_000),
            ))

        # No-arb forward (q ≈ 0 for index)
        no_arb_forward = self._spot * math.exp(self.rfr * t)

        # Maybe inject a mispricing
        if self._rng.random() < self.mispricing_prob:
            kind = self._rng.choice(self.mispricing_kinds)
            quotes = self._inject_mispricing(quotes, kind, atm, no_arb_forward)

        # ALSO occasionally mis-mark the future to create a parity/conversion sig
        if self._rng.random() < self.mispricing_prob * 0.5:
            no_arb_forward *= (1 + self._rng.uniform(-0.0008, 0.0008))

        return ChainSnapshot(
            underlying=self.underlying,
            expiry=self._expiry,
            spot=self._spot,
            ts=datetime.now(IST),
            risk_free_rate=self.rfr,
            quotes=tuple(quotes),
            lot_size=self._lot_size,
            futures_price=no_arb_forward,
        )

    def _inject_mispricing(
        self,
        quotes: list[ChainQuote],
        kind: str,
        atm: float,
        forward: float,
    ) -> list[ChainQuote]:
        """Perturb specific quotes to create a real opportunity."""
        if kind == "box":
            # Mark the lower-strike call ASK down → buyable cheap → long-box edge
            target_strike = float(atm)
            for i, q in enumerate(quotes):
                if q.strike == target_strike and q.option_type == OptionType.CALL:
                    edge = self._rng.uniform(2.0, 10.0)
                    quotes[i] = ChainQuote(
                        strike=q.strike, option_type=q.option_type,
                        last_price=q.last_price, bid=q.bid,
                        ask=max((q.ask or q.last_price) - edge, 0.05),
                        iv=q.iv, volume=q.volume, open_interest=q.open_interest,
                    )
                    break
        elif kind == "adjacent":
            # Inflate higher-strike CE bid > lower-strike CE ask
            calls = sorted([(i, q) for i, q in enumerate(quotes) if q.option_type == OptionType.CALL],
                           key=lambda x: x[1].strike)
            if len(calls) >= 4:
                # Pick a far-OTM pair (where market makers don't quote tight)
                pair_idx = self._rng.randint(len(calls) // 2 + 2, len(calls) - 2)
                low_i, low_q = calls[pair_idx - 1]
                high_i, high_q = calls[pair_idx]
                bump = self._rng.uniform(20.0, 80.0)
                quotes[high_i] = ChainQuote(
                    strike=high_q.strike, option_type=high_q.option_type,
                    last_price=high_q.last_price + bump,
                    bid=(high_q.bid or high_q.last_price) + bump,
                    ask=(high_q.ask or high_q.last_price) + bump,
                    iv=high_q.iv, volume=high_q.volume, open_interest=high_q.open_interest,
                )
        elif kind == "parity":
            # Inflate one ATM call → put-call parity violation
            target_strike = float(atm)
            for i, q in enumerate(quotes):
                if q.strike == target_strike and q.option_type == OptionType.CALL:
                    bump = self._rng.uniform(8.0, 25.0)
                    quotes[i] = ChainQuote(
                        strike=q.strike, option_type=q.option_type,
                        last_price=q.last_price + bump,
                        bid=(q.bid or q.last_price) + bump,
                        ask=(q.ask or q.last_price) + bump,
                        iv=q.iv, volume=q.volume, open_interest=q.open_interest,
                    )
                    break
        return quotes


@dataclass
class LiveBrokerChainSource:
    """Wraps a ChainBuilder so it satisfies the ChainSource protocol.

    Polls the builder's `snapshot()` every `interval_seconds`.
    """

    builder: object   # qbacktest.data.ChainBuilder; loose typed to avoid circular import
    interval_seconds: float = 1.0
    _last_yield_ts: Optional[datetime] = field(default=None, init=False)

    async def start(self) -> None:
        await self.builder.start()  # type: ignore[attr-defined]

    async def stop(self) -> None:
        await self.builder.stop()   # type: ignore[attr-defined]

    async def next(self) -> ChainSnapshot:
        if self._last_yield_ts is not None:
            elapsed = (datetime.now(IST) - self._last_yield_ts).total_seconds()
            remaining = self.interval_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        # Wait until the builder has both spot and at least some option quotes
        while True:
            snap = self.builder.snapshot()  # type: ignore[attr-defined]
            if snap is not None and len(snap.quotes) > 0:
                self._last_yield_ts = datetime.now(IST)
                return snap
            await asyncio.sleep(0.2)
