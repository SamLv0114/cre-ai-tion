"""Get schema-valid JSON out of a model, or fail cleanly.

Order: parse (also pulls JSON out of prose or a code fence, no extra call) -> validate with Pydantic ->
ask the same model to fix its reply, up to max_repairs -> next model in the chain -> raise, so the agent
can use its deterministic fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from ..domain.schemas import Usage
from ..observability.tracing import Tracer
from ..prompts import REPAIR_PROMPT
from ..reliability.budget import Budget
from ..reliability.errors import CircuitOpenError, LLMError, StructuredOutputError
from .base import LLMRequest
from .router import ModelRouter

T = TypeVar("T", bound=BaseModel)
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.S)


@dataclass
class StructuredResult(Generic[T]):
    value: T
    model: str
    provider: str
    usage: Usage
    repairs: int
    fallback_depth: int
    normalized: bool
    log: list[str] = field(default_factory=list)


def extract_json(text: str) -> tuple[Any, bool]:
    """Return (object, normalized). Raises json.JSONDecodeError when no JSON value can be parsed."""
    s = text.strip()
    try:
        # clean JSON, the normal case
        return json.loads(s), False
    except json.JSONDecodeError:
        pass
    # JSON inside a ```json fence
    m = _FENCE.search(s)
    if m:
        try:
            return json.loads(m.group(1)), True
        except json.JSONDecodeError:
            pass
    # JSON after some chatter: parse from the first brace with the real JSON parser
    start = s.find("{")
    if start < 0:
        raise json.JSONDecodeError("no JSON object in reply", s, 0)
    obj, _ = json.JSONDecoder().raw_decode(s[start:])
    return obj, True


# short error text we send back to the model when asking it to fix its reply
def feedback(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:8]]
        return "schema validation failed -> " + "; ".join(parts)
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON ({exc.msg} at char {exc.pos})"
    return f"{type(exc).__name__}: {exc}"


async def call_structured(
    router: ModelRouter,
    tracer: Tracer,
    *,
    role: str,
    schema: type[T],
    messages: list[dict[str, str]],
    payload: dict[str, Any],
    variant: str,
    max_repairs: int,
    budget: Optional[Budget] = None,
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> StructuredResult[T]:
    usage = Usage()
    log: list[str] = []
    # outer loop: models in the fallback chain; inner loop: repair attempts on the same model
    for depth, route in enumerate(router.chain(role)):
        msgs = list(messages)
        for repair in range(max_repairs + 1):
            req = LLMRequest(role=role, model=route.model, messages=msgs, temperature=temperature,
                             max_tokens=max_tokens, json_mode=True, payload=payload, variant=variant)
            with tracer.span("llm.call", role=role, model=route.model, provider=route.provider, repair=repair,
                             fallback_depth=depth) as sp:
                try:
                    resp = await router.complete(route, req)
                except CircuitOpenError:
                    # model known to be down (or over spend cap): skip it
                    tracer.event("circuit", f"{route.provider}:{route.model} circuit open -> next route")
                    log.append(f"{route.model}: circuit open")
                    break
                except LLMError as exc:
                    # retries already used up inside the router: try the next model
                    tracer.event("fallback", f"{route.provider}:{route.model} gave up ({type(exc).__name__}) "
                                             f"-> next route in '{role}' chain")
                    log.append(f"{route.model}: {type(exc).__name__}")
                    break
                u = Usage(prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                          cost_cny=router.cost_cny(route.model, resp), calls=1, latency_ms=resp.latency_ms)
                usage = usage + u
                sp.set(tokens=u.total_tokens, cost_cny=u.cost_cny, latency_ms=resp.latency_ms)
                if budget is not None:
                    budget.charge(u)  # BudgetExceeded propagates: the planner stops spending on this variant
                try:
                    obj, normalized = extract_json(resp.text)
                    value = schema.model_validate(obj)
                except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                    # bad reply: show the model its own answer plus what was wrong, and ask again
                    fb = feedback(exc)
                    last = repair == max_repairs
                    tracer.event("schema.invalid", f"{route.model} reply rejected: {fb[:150]} -> "
                                                   + ("repairs exhausted" if last else f"repair {repair + 1}/{max_repairs}"))
                    log.append(f"{route.model}: invalid ({fb[:80]})")
                    sp.set(valid=False)
                    msgs = msgs + [{"role": "assistant", "content": resp.text[:6000]},
                                   {"role": "user", "content": REPAIR_PROMPT.format(errors=fb)}]
                    continue
                sp.set(valid=True)
                if normalized:
                    tracer.event("json.normalized", "JSON recovered from prose/code fence by the parser; no repair call")
                if repair:
                    tracer.event("repair.ok", f"{route.model} returned valid {schema.__name__} after {repair} repair(s)")
                if depth:
                    tracer.event("repair.ok", f"served by fallback route #{depth} ({route.provider}:{route.model})")
                return StructuredResult(value=value, model=route.model, provider=route.provider, usage=usage,
                                        repairs=repair, fallback_depth=depth, normalized=normalized, log=log)
    # nothing worked; the calling agent falls back to its deterministic default
    err = StructuredOutputError(f"no valid {schema.__name__} from any route for '{role}'", log)
    err.usage = usage  # type: ignore[attr-defined]
    raise err
