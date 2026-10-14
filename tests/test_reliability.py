"""Retries, timeouts, breakers, limiter, structured-output repair ladder, tool degradation."""

import asyncio
import json

import pytest

from creaition.config import RunConfig
from creaition.domain.schemas import DesignIntent
from creaition.llm.faulty import FaultyLLM
from creaition.llm.mock import MockLLM
from creaition.llm.router import ModelRouter
from creaition.llm.structured import call_structured, extract_json
from creaition.observability.tracing import NullTracer
from creaition.reliability.circuit import BreakerRegistry, CircuitBreaker
from creaition.reliability.errors import (
    CircuitOpenError, LLMAuthError, StructuredOutputError, ToolError, ToolTimeout,
)
from creaition.reliability.faults import FaultConfig, FaultInjector, ScriptedFault
from creaition.reliability.limiter import AdaptiveLimiter
from creaition.reliability.retry import RetryPolicy, retry_async
from creaition.tools.registry import ToolExecutor

FAST = RetryPolicy(max_attempts=3, base_delay_s=0.001, max_delay_s=0.005, timeout_s=0.2)


def test_retry_recovers_from_transient_errors():
    calls = []

    async def fn(n):
        calls.append(n)
        if n < 3:
            raise ToolError("503")
        return "ok"

    assert asyncio.run(retry_async(fn, FAST)) == "ok"
    assert calls == [1, 2, 3]


def test_retry_does_not_retry_permanent_errors():
    calls = []

    async def fn(n):
        calls.append(n)
        raise LLMAuthError("401")

    with pytest.raises(LLMAuthError):
        asyncio.run(retry_async(fn, FAST))
    assert calls == [1]


def test_per_attempt_timeout_is_enforced_and_typed():
    async def hang(n):
        await asyncio.sleep(5)

    with pytest.raises(ToolTimeout):
        asyncio.run(retry_async(hang, RetryPolicy(max_attempts=2, base_delay_s=0.001, timeout_s=0.05)))


def test_backoff_is_bounded():
    p = RetryPolicy(base_delay_s=0.1, max_delay_s=0.3)
    assert all(0 <= p.backoff(a) <= 0.3 for a in range(1, 20))


def test_circuit_breaker_lifecycle():
    now = [0.0]
    b = CircuitBreaker("x", failure_threshold=2, reset_timeout_s=10, clock=lambda: now[0])
    b.record_failure()
    b.allow()
    b.record_failure()
    with pytest.raises(CircuitOpenError):
        b.allow()
    now[0] = 11
    b.allow()  # half-open probe allowed
    with pytest.raises(CircuitOpenError):
        b.allow()  # only one probe in flight
    b.record_success()
    assert b.state == "closed"
    b.record_failure(force_open=True)
    assert b.state == "open"


def test_aimd_limiter_halves_on_throttle():
    lim = AdaptiveLimiter(initial=8, increase_after=2)
    lim.on_throttle()
    assert lim.limit == 4
    lim.on_success()
    lim.on_success()
    assert lim.limit == 5


def test_extract_json_handles_prose_and_fences_but_not_truncation():
    obj = {"a": 1}
    assert extract_json(json.dumps(obj)) == (obj, False)
    assert extract_json(f"Here you go:\n```json\n{json.dumps(obj)}\n```") == (obj, True)
    assert extract_json(f"Sure! {json.dumps(obj)} hope it helps") == (obj, True)
    with pytest.raises(json.JSONDecodeError):
        extract_json('{"a": 1, "b": [1, 2')


def _router(faults: FaultConfig, **cfg_kw):
    cfg = RunConfig(faults=faults, **cfg_kw)
    tracer = NullTracer()
    inj = FaultInjector(faults)
    mock = FaultyLLM(MockLLM(time_scale=0.0), inj, tracer, hang_s=5)
    clients = {"deepseek": mock, "deepseek_anthropic": mock, "openai": mock}
    return ModelRouter(cfg, clients, BreakerRegistry(2, 60), tracer), cfg, inj


PAYLOAD = {"brief": {"brief_id": "B9", "title": "t", "text": "Rugged outdoor detector, IP65, must survive a 1.5 m drop."}}
MSGS = [{"role": "user", "content": "interpret"}]


@pytest.mark.parametrize("kind", ["malformed_truncated", "malformed_types", "malformed_missing"])
def test_schema_repair_recovers_malformed_output(kind):
    faults = FaultConfig(enabled=True, scripted=[ScriptedFault(target="llm", name="brief_interpreter", calls=[1],
                                                               kind=kind)])
    router, cfg, inj = _router(faults)
    res = asyncio.run(call_structured(router, NullTracer(), role="brief_interpreter", schema=DesignIntent,
                                      messages=MSGS, payload=PAYLOAD, variant="B9", max_repairs=2))
    assert res.repairs == 1 and res.fallback_depth == 0
    assert inj.injected[0]["kind"] == kind


def test_prose_wrapped_json_needs_no_repair_call():
    faults = FaultConfig(enabled=True, scripted=[ScriptedFault(target="llm", name="brief_interpreter", calls=[1],
                                                               kind="malformed_prose")])
    router, _, _ = _router(faults)
    res = asyncio.run(call_structured(router, NullTracer(), role="brief_interpreter", schema=DesignIntent,
                                      messages=MSGS, payload=PAYLOAD, variant="B9", max_repairs=2))
    assert res.repairs == 0 and res.normalized and res.usage.calls == 1


def test_model_outage_falls_back_to_next_route_and_opens_breaker():
    faults = FaultConfig(enabled=True, model_outages=["deepseek-v4-pro"])
    router, cfg, _ = _router(faults)
    for i in range(3):
        res = asyncio.run(call_structured(router, NullTracer(), role="brief_interpreter", schema=DesignIntent,
                                          messages=MSGS, payload=PAYLOAD, variant=f"B9-{i}", max_repairs=1))
        assert res.model == "deepseek-flash" and res.fallback_depth == 1
    assert router.breakers.snapshot()["llm:deepseek:deepseek-v4-pro"] == "open"


def test_all_routes_failing_raises_structured_error():
    faults = FaultConfig(enabled=True, scripted=[ScriptedFault(target="llm", name="brief_interpreter",
                                                               calls=list(range(1, 50)), kind="malformed_truncated")])
    router, _, _ = _router(faults)
    with pytest.raises(StructuredOutputError):
        asyncio.run(call_structured(router, NullTracer(), role="brief_interpreter", schema=DesignIntent,
                                    messages=MSGS, payload=PAYLOAD, variant="B9", max_repairs=1))


def test_tool_timeout_degrades_instead_of_crashing():
    faults = FaultConfig(enabled=True, scripted=[ScriptedFault(target="tool", name="materials_lookup",
                                                               calls=[1, 2, 3], kind="timeout")])
    ex = ToolExecutor(FAST, FaultInjector(faults), BreakerRegistry(5, 60), NullTracer(), hang_s=5)

    async def live():
        return "live"

    res = asyncio.run(ex.invoke("materials_lookup", live, variant="v1", fallback=lambda: "snapshot"))
    assert res.degraded and res.value == "snapshot" and res.attempts == 3


def test_flaky_tool_recovers_by_retry():
    faults = FaultConfig(enabled=True, scripted=[ScriptedFault(target="tool", name="dfm_validate", calls=[1],
                                                               kind="tool_error")])
    ex = ToolExecutor(FAST, FaultInjector(faults), BreakerRegistry(5, 60), NullTracer())

    async def live():
        return "ok"

    res = asyncio.run(ex.invoke("dfm_validate", live, variant="v1", fallback=lambda: "local"))
    assert not res.degraded and res.value == "ok" and res.attempts == 2


@pytest.mark.parametrize("status,cls,retryable", [
    (401, "LLMAuthError", False), (403, "LLMAuthError", False), (429, "LLMRateLimited", True),
    (503, "LLMServerError", True), (400, "LLMBadRequest", False),
])
def test_live_http_errors_are_classified(status, cls, retryable):
    from creaition.llm.providers import _classify_http
    err = _classify_http(status, "body")
    assert type(err).__name__ == cls and err.retryable is retryable


def test_provider_spend_cap_skips_vendor_and_falls_back():
    from creaition.config import RunConfig as RC
    cfg = RC(provider_spend_cap_cny={"deepseek": 0.0})  # cap already reached for the primary vendor
    tracer = NullTracer()
    mock = FaultyLLM(MockLLM(time_scale=0.0), FaultInjector(FaultConfig()), tracer)
    router = ModelRouter(cfg, {"deepseek": mock, "deepseek_anthropic": mock, "openai": mock}, BreakerRegistry(2, 60), tracer)
    res = asyncio.run(call_structured(router, tracer, role="brief_interpreter", schema=DesignIntent, messages=MSGS,
                                      payload=PAYLOAD, variant="B9", max_repairs=1))
    assert res.provider == "deepseek_anthropic"  # deepseek routes skipped by the cap; fell through to the next route


def test_reviser_tool_args_are_validated_not_crashing():
    # live regression: the model sent {"drop_secondary_ops": true}; it must come back as a tool error, not a TypeError
    from creaition.domain.schemas import TOOL_ARGS
    from pydantic import ValidationError as VE
    with pytest.raises(VE):
        TOOL_ARGS["cost_what_if"].model_validate({"material": "ABS", "drop_secondary_ops": True})
    ok = TOOL_ARGS["cost_what_if"].model_validate({"material": "ABS", "drop_secondary_ops": ["paint"]})
    assert ok.drop_secondary_ops[0].value == "paint"


def test_cancelled_probe_does_not_leave_breaker_stuck_half_open():
    # a step timeout cancels the half-open probe mid-call; the next call must still be allowed to probe
    breakers = BreakerRegistry(1, 0.0)
    breakers.get("tool:slow").record_failure()  # open; cool-down of 0 s -> next call is the probe
    ex = ToolExecutor(RetryPolicy(max_attempts=1, timeout_s=5), FaultInjector(FaultConfig()), breakers, NullTracer())

    async def hang():
        await asyncio.sleep(5)

    async def cancelled_probe():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ex.invoke("slow", hang, variant="v1", fallback=lambda: None), 0.02)

    asyncio.run(cancelled_probe())
    assert breakers.get("tool:slow").allow() is True  # free to probe again, not "probe in flight" forever
