"""Circuit breaker (closed -> open -> half-open) shared by every caller of a dependency.

One breaker per model endpoint and per tool. When a provider is down, the first
few variants pay the timeout cost; every later variant fails fast and goes
straight to the fallback route instead of each burning its full retry budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import CircuitOpenError


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    reset_timeout_s: float = 5.0
    clock: Callable[[], float] = time.monotonic
    on_transition: Optional[Callable[[str, str, str], None]] = None
    state: str = field(default="closed", init=False)
    consecutive_failures: int = field(default=0, init=False)
    opened_at: float = field(default=0.0, init=False)
    half_open_in_flight: bool = field(default=False, init=False)

    def _set(self, new: str) -> None:
        if new != self.state:
            old, self.state = self.state, new
            if self.on_transition:
                self.on_transition(self.name, old, new)

    def allow(self) -> bool:
        """Raise CircuitOpenError unless a call may proceed. Returns True if this call is the half-open probe."""
        if self.state == "open":
            # after the cool-down, let one test call through (half-open)
            if self.clock() - self.opened_at >= self.reset_timeout_s:
                self._set("half_open")
            else:
                raise CircuitOpenError(f"circuit '{self.name}' is open")
        if self.state == "half_open":
            # only one probe at a time while half-open
            if self.half_open_in_flight:
                raise CircuitOpenError(f"circuit '{self.name}' is half-open (probe in flight)")
            self.half_open_in_flight = True
            return True
        return False

    # a probe that was cancelled (e.g. by a step timeout) proved nothing either way: free the slot so the
    # next call can probe, instead of leaving the breaker stuck half-open and rejecting everything
    def release_probe(self) -> None:
        self.half_open_in_flight = False

    # any success closes the breaker again
    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.half_open_in_flight = False
        self._set("closed")

    def record_failure(self, *, force_open: bool = False) -> None:
        self.consecutive_failures += 1
        self.half_open_in_flight = False
        # a failed probe re-opens straight away; force_open is used for auth errors
        if force_open or self.state == "half_open" or self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self.clock()
            self._set("open")


# one breaker per name, e.g. 'llm:deepseek:deepseek-v4-pro' or 'tool:dfm_validate'
class BreakerRegistry:
    def __init__(self, failure_threshold: int, reset_timeout_s: float,
                 on_transition: Optional[Callable[[str, str, str], None]] = None):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._threshold = failure_threshold
        self._reset = reset_timeout_s
        self._on_transition = on_transition

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, self._threshold, self._reset,
                                                  on_transition=self._on_transition)
        return self._breakers[name]

    def snapshot(self) -> dict[str, str]:
        return {k: b.state for k, b in self._breakers.items()}
