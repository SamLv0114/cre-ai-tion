"""Backpressure primitives: per-model token bucket + AIMD adaptive concurrency.

- TokenBucket caps request *rate* to stay under the provider's RPM.
- AdaptiveLimiter caps *in-flight* requests; it halves on a 429 and grows by one
  after a streak of successes (TCP-style AIMD), so a fan-out of thousands of
  variants converges to what the provider actually tolerates instead of
  hammering it into a retry storm.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int):
        self.rate = rate_per_s
        self.capacity = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        # created on first use, inside the running event loop (Python 3.9 binds them to a loop when created)
        self._lock: Optional[asyncio.Lock] = None

    async def acquire(self) -> float:
        """Wait for one token; returns seconds waited (exported as a backpressure metric)."""
        waited = 0.0
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = time.monotonic()
                # refill based on time since the last call
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                # not enough tokens yet: sleep until one is ready
                delay = (1 - self.tokens) / self.rate
                waited += delay
                await asyncio.sleep(delay)


class AdaptiveLimiter:
    def __init__(self, initial: int, minimum: int = 1, maximum: int = 64, increase_after: int = 10):
        self.limit = initial
        self.minimum = minimum
        self.maximum = maximum
        self.increase_after = increase_after
        self.in_flight = 0
        self._streak = 0
        self._cond_: Optional[asyncio.Condition] = None

    @property
    def _cond(self) -> asyncio.Condition:
        # created on first use, inside the running event loop (see TokenBucket)
        if self._cond_ is None:
            self._cond_ = asyncio.Condition()
        return self._cond_

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._cond:
            # block until there's a free slot
            await self._cond.wait_for(lambda: self.in_flight < self.limit)
            self.in_flight += 1
        try:
            yield
        finally:
            async with self._cond:
                self.in_flight -= 1
                self._cond.notify_all()

    # after a run of successes, allow one more call in flight
    def on_success(self) -> None:
        self._streak += 1
        if self._streak >= self.increase_after and self.limit < self.maximum:
            self.limit += 1
            self._streak = 0

    # got a 429: halve the number of calls in flight
    def on_throttle(self) -> None:
        self._streak = 0
        self.limit = max(self.minimum, self.limit // 2)
