"""Tool execution with timeout, retry, circuit breaker and graceful degradation.

A tool call never crashes a variant. If the live service fails after retries
(or its breaker is open) the executor returns the declared fallback and marks
the result `degraded`; downstream gates treat degraded evidence as "needs a
human", never as a silent pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Optional, TypeVar

from ..observability.tracing import Tracer
from ..reliability.circuit import BreakerRegistry
from ..reliability.errors import CircuitOpenError, ToolError, ToolTimeout
from ..reliability.faults import FaultInjector
from ..reliability.retry import RetryPolicy, retry_async

T = TypeVar("T")


# tool output plus where it came from (live service or fallback)
@dataclass
class ToolResult(Generic[T]):
    value: T
    source: str  # "live" | "fallback"
    degraded: bool
    attempts: int
    error: Optional[str] = None


class ToolExecutor:
    def __init__(self, policy: RetryPolicy, injector: FaultInjector, breakers: BreakerRegistry, tracer: Tracer,
                 hang_s: float = 30.0):
        self.policy = policy
        self.injector = injector
        self.breakers = breakers
        self.tracer = tracer
        self.hang_s = hang_s

    async def invoke(self, name: str, primary: Callable[[], Awaitable[T]], *, variant: str,
                     fallback: Optional[Callable[[], T]] = None, fallback_desc: str = "fallback") -> ToolResult[T]:
        breaker = self.breakers.get(f"tool:{name}")
        attempts, probe = 0, False
        with self.tracer.span("tool.call", tool=name) as sp:
            try:
                # service known to be down: skip straight to the fallback
                probe = breaker.allow()

                async def attempt(n: int) -> T:
                    nonlocal attempts
                    attempts = n
                    # fault injection happens here, so the same recovery code runs in tests and demos
                    kind = self.injector.decide_tool(name, variant)
                    if kind:
                        self.tracer.event("fault.injected", f"{kind} on tool '{name}' (attempt {n})", kind=kind)
                    if kind == "timeout":
                        await asyncio.sleep(self.hang_s)
                    elif kind == "tool_error":
                        raise ToolError(f"injected 503 from {name} service")
                    return await primary()

                def on_retry(n: int, exc: BaseException, delay: float) -> None:
                    self.tracer.event("retry", f"tool '{name}' attempt {n} failed ({type(exc).__name__}); "
                                               f"backoff {delay * 1000:.0f} ms")

                value = await retry_async(attempt, self.policy, timeout_exc=ToolTimeout, on_retry=on_retry)
                breaker.record_success()
                if attempts > 1:
                    self.tracer.event("repair.ok", f"tool '{name}' succeeded on attempt {attempts}")
                sp.set(attempts=attempts, source="live")
                return ToolResult(value=value, source="live", degraded=False, attempts=attempts)
            except CircuitOpenError as exc:
                error = str(exc)
            except asyncio.CancelledError:  # BaseException: free the half-open probe slot, then let it cancel
                if probe:
                    breaker.release_probe()
                raise
            except Exception as exc:  # noqa: BLE001 - every tool failure degrades, none crash the variant
                breaker.record_failure()
                error = f"{type(exc).__name__}: {exc}"
            # no fallback for this tool: the error has to go up
            if fallback is None:
                sp.set(attempts=attempts, source="none")
                raise ToolError(f"{name} failed with no fallback: {error}")
            # use the fallback data and flag it; the gate will send this variant to a human
            self.tracer.event("tool.degraded", f"'{name}' unavailable ({error[:80]}) -> {fallback_desc}")
            sp.set(attempts=attempts, source="fallback", degraded=True)
            return ToolResult(value=fallback(), source="fallback", degraded=True, attempts=attempts, error=error)
