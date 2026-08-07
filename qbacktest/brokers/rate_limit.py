"""Token-bucket rate limiter for broker REST APIs.

Upstox quotas (v2):
    50 req/sec, 500 req/min, 2000 req/30 min

Fyers quotas (v3):
    10 req/sec, 200 req/min, 100000 req/day

We model multiple buckets per broker; a request must acquire from ALL applicable
buckets before proceeding.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    """Classic token bucket. Capacity = max burst; refill_rate in tokens/second."""

    capacity: float
    refill_rate: float       # tokens per second
    tokens: float = 0.0
    last_refill: float = 0.0

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def time_until_available(self, n: float = 1.0) -> float:
        self._refill()
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.refill_rate


class CompositeRateLimiter:
    """Composite of several buckets — request must satisfy ALL."""

    def __init__(self, buckets: list[TokenBucket]) -> None:
        self.buckets = buckets
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                if all(b.tokens >= n or b.try_acquire(0) for b in self.buckets):
                    if all(b.try_acquire(n) for b in self.buckets):
                        return
                wait = max(b.time_until_available(n) for b in self.buckets)
                # Fall back to a small sleep if all buckets temporarily unavailable
                await asyncio.sleep(max(0.005, wait))


def upstox_limits() -> CompositeRateLimiter:
    return CompositeRateLimiter([
        TokenBucket(capacity=50, refill_rate=50),                       # 50/sec
        TokenBucket(capacity=500, refill_rate=500 / 60.0),              # 500/min
        TokenBucket(capacity=2000, refill_rate=2000 / 1800.0),          # 2000/30min
    ])


def fyers_limits() -> CompositeRateLimiter:
    return CompositeRateLimiter([
        TokenBucket(capacity=10, refill_rate=10),                       # 10/sec
        TokenBucket(capacity=200, refill_rate=200 / 60.0),              # 200/min
        TokenBucket(capacity=100_000, refill_rate=100_000 / 86400.0),   # 100k/day
    ])
