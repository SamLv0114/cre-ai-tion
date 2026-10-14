"""Retry with exponential backoff, full jitter and a per-attempt timeout."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from .errors import LLMTimeout, PipelineError, ToolTimeout

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 2.0
    timeout_s: float = 30.0

    def backoff(self, attempt: int) -> float:
        """Full jitter (AWS architecture blog): sleep U(0, min(cap, base * 2^attempt))."""
        return random.uniform(0, min(self.max_delay_s, self.base_delay_s * (2 ** attempt)))


# our own errors say whether they're worth retrying; for anything else, only timeouts and dropped connections are
def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, PipelineError):
        return exc.retryable
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError))


async def retry_async(
    fn: Callable[[int], Awaitable[T]],
    policy: RetryPolicy,
    *,
    timeout_exc: type[PipelineError] = ToolTimeout,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Call `fn(attempt)` until it succeeds, a non-retryable error occurs, or attempts run out.

    Each attempt is bounded by `policy.timeout_s`; a timeout is converted into
    `timeout_exc` (retryable) so callers see one error vocabulary.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            # each attempt gets its own time limit
            return await asyncio.wait_for(fn(attempt), timeout=policy.timeout_s)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # turn a raw timeout into our typed (retryable) error
            last = timeout_exc(f"attempt {attempt} exceeded {policy.timeout_s:.2f}s")
            last.__cause__ = exc
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
        # give up on permanent errors (e.g. 401) or when out of attempts
        if not is_retryable(last) or attempt == policy.max_attempts:
            raise last
        delay = policy.backoff(attempt)
        if on_retry:
            on_retry(attempt, last, delay)
        await asyncio.sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises


__all__ = ["RetryPolicy", "retry_async", "is_retryable", "LLMTimeout", "ToolTimeout"]
