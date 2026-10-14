"""Shared agent plumbing.

An agent is a function from typed inputs to one typed artifact plus metadata.
Agents hold no state between calls and never touch the store; persistence,
retries at step level and routing decisions belong to the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Optional, TypeVar

from ..config import RunConfig
from ..domain.schemas import Usage
from ..llm.router import ModelRouter
from ..llm.structured import StructuredResult
from ..observability.tracing import Tracer
from ..state.memory import MemorySnapshot, SemanticCache
from ..tools.services import Tools

T = TypeVar("T")


# everything an agent may use; passed in, never global
@dataclass
class AgentContext:
    cfg: RunConfig
    router: ModelRouter
    tools: Tools
    tracer: Tracer
    memory: MemorySnapshot
    semantic_cache: Optional[SemanticCache]
    run_id: str


# what every agent returns: the artifact (or None) plus usage, model used and any fallback flags
@dataclass
class AgentResult(Generic[T]):
    artifact: Optional[T]
    usage: Usage = field(default_factory=Usage)
    model: Optional[str] = None
    degraded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # copy repair/fallback info from the model call into short notes for the trace
    @classmethod
    def from_structured(cls, res: StructuredResult, artifact: T, **kw: Any) -> "AgentResult[T]":
        notes = []
        if res.repairs:
            notes.append(f"schema repaired x{res.repairs}")
        if res.fallback_depth:
            notes.append(f"fallback route #{res.fallback_depth}")
        if res.normalized:
            notes.append("json normalized")
        return cls(artifact=artifact, usage=res.usage, model=res.model, notes=notes + kw.pop("notes", []), **kw)
