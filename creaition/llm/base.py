"""Provider-neutral LLM request/response types and client protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class LLMRequest:
    role: str  # agent role, e.g. "concept_generator"
    model: str
    messages: list[dict[str, str]]
    temperature: float = 0.2
    max_tokens: int = 2048
    json_mode: bool = True
    # Structured copy of the inputs already serialised into `messages`. Real
    # clients ignore it; the offline mock uses it to synthesise a response.
    payload: dict[str, Any] = field(default_factory=dict)
    variant: str = "-"
    attempt: int = 1
    extra: dict[str, Any] = field(default_factory=dict)  # route-level provider params


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    raw: Optional[dict[str, Any]] = None


class LLMClient(Protocol):
    provider: str

    async def complete(self, req: LLMRequest) -> LLMResponse: ...


# rough count, ~3.5 characters per token
def estimate_tokens(text: str) -> int:
    # ~3.5 chars/token for mixed English/JSON; only used when a provider omits usage.
    return max(1, int(len(text) / 3.5))
