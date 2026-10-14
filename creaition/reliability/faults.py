"""Fault injection for model calls and tools, applied at the client / tool boundary so tests, demos and live
runs all go through the same recovery code.

Faults are either scripted ("fail the 1st call for variant v001") or random at a fixed, seeded rate.
"""

from __future__ import annotations

import json
import random
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .idempotency import stable_int

FaultKind = Literal[
    "timeout", "server_error", "rate_limit", "auth_error",
    "malformed_truncated", "malformed_prose", "malformed_types", "malformed_missing",
    "tool_error",
]


class ScriptedFault(BaseModel):
    target: Literal["llm", "tool"]
    name: str = "*"  # llm role or tool name
    variant: str = "*"  # suffix match on variant id
    model: str = "*"
    calls: list[int] = Field(default_factory=lambda: [1])  # 1-based call numbers per (target, name, variant, model)
    kind: FaultKind


class FaultConfig(BaseModel):
    enabled: bool = False
    seed: int = 7
    llm_malformed_rate: float = 0.0
    llm_timeout_rate: float = 0.0
    llm_rate_limit_rate: float = 0.0
    llm_server_error_rate: float = 0.0
    tool_error_rate: float = 0.0
    tool_timeout_rate: float = 0.0
    model_outages: list[str] = Field(default_factory=list)  # models that always return 503
    scripted: list[ScriptedFault] = Field(default_factory=list)


_MALFORMED = ("malformed_truncated", "malformed_prose", "malformed_types", "malformed_missing")


class FaultInjector:
    def __init__(self, cfg: FaultConfig):
        self.cfg = cfg
        self._counters: dict[tuple, int] = {}
        self.injected: list[dict] = []

    # count calls per (target, name, variant, model) so scripts can say 'fail the 1st call'
    def _count(self, key: tuple) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    # first scripted fault that matches this call, if any
    def _scripted(self, target: str, name: str, variant: str, model: str, n: int) -> Optional[str]:
        for f in self.cfg.scripted:
            if f.target != target:
                continue
            if f.name not in ("*", name) or f.model not in ("*", model):
                continue
            if f.variant != "*" and not variant.endswith(f.variant):
                continue
            if n in f.calls:
                return f.kind
        return None

    def decide_llm(self, role: str, model: str, variant: str) -> Optional[str]:
        if not self.cfg.enabled:
            return None
        n = self._count(("llm", role, variant, model))
        kind = self._scripted("llm", role, variant, model, n)
        # a model listed as 'down' fails every call
        if kind is None and model in self.cfg.model_outages:
            kind = "server_error"
        if kind is None:
            # random faults use a seeded draw so a chaos run is repeatable
            rng = random.Random(stable_int(self.cfg.seed, "llm", role, model, variant, n))
            r = rng.random()
            c = self.cfg
            # rates are stacked into ranges: [timeout | 429 | 5xx | malformed | ok]
            edges = [
                (c.llm_timeout_rate, "timeout"),
                (c.llm_rate_limit_rate, "rate_limit"),
                (c.llm_server_error_rate, "server_error"),
                (c.llm_malformed_rate, rng.choice(_MALFORMED)),
            ]
            acc = 0.0
            for rate, k in edges:
                acc += rate
                if r < acc:
                    kind = k
                    break
        if kind:
            self.injected.append({"target": "llm", "name": role, "model": model, "variant": variant, "call": n,
                                  "kind": kind})
        return kind

    def decide_tool(self, tool: str, variant: str) -> Optional[str]:
        if not self.cfg.enabled:
            return None
        n = self._count(("tool", tool, variant))
        kind = self._scripted("tool", tool, variant, "*", n)
        if kind is None:
            rng = random.Random(stable_int(self.cfg.seed, "tool", tool, variant, n))
            r = rng.random()
            if r < self.cfg.tool_timeout_rate:
                kind = "timeout"
            elif r < self.cfg.tool_timeout_rate + self.cfg.tool_error_rate:
                kind = "tool_error"
        if kind:
            self.injected.append({"target": "tool", "name": tool, "variant": variant, "call": n, "kind": kind})
        return kind


def corrupt(text: str, kind: str) -> str:
    """Turn a valid JSON completion into the requested flavour of malformed output."""
    # cut the reply off half way, like a dropped stream
    if kind == "malformed_truncated":
        return text[: max(1, int(len(text) * 0.55))]
    # wrap the JSON in chatty text and a code fence
    if kind == "malformed_prose":
        return f"Sure! Here is the specification you asked for:\n```json\n{text}\n```\nLet me know if you need changes."
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text[: len(text) // 2]
    if not isinstance(obj, dict) or not obj:
        return text[: len(text) // 2]
    # drop the first field so schema validation fails
    if kind == "malformed_missing":
        obj.pop(next(iter(obj)))
        return json.dumps(obj)
    if kind == "malformed_types":
        _stringify_first_number(obj)
        return json.dumps(obj)
    return text


# turn the first number into text like 'about 2.0 mm' (wrong type)
def _stringify_first_number(node) -> bool:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                node[k] = f"about {v} mm"
                return True
            if _stringify_first_number(v):
                return True
    elif isinstance(node, list):
        for item in node:
            if _stringify_first_number(item):
                return True
    return False
