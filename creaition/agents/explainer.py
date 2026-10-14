"""Explainer (LLM) for the Ask feature: answers questions about a run from retrieved facts only."""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain.schemas import Answer
from ..llm.structured import call_structured
from ..prompts import render
from ..reliability.errors import StructuredOutputError
from .base import AgentContext, AgentResult

ROLE = "explainer"


async def run(ctx: AgentContext, question: str, facts: list[str]) -> AgentResult[Answer]:
    inputs = {"question": question, "facts": facts}
    messages, _ = render(ROLE, ctx.cfg.prompt_version, inputs, Answer)
    try:
        res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=Answer, messages=messages,
                                    payload=inputs, variant="ask", max_repairs=ctx.cfg.max_repairs, temperature=0.0)
    except StructuredOutputError:
        # Deterministic fallback: the retrieved facts are themselves the answer.
        return AgentResult(artifact=Answer(answer="(model unavailable) Relevant records:\n- " + "\n- ".join(facts[:8])
                                           if facts else "(model unavailable) No matching records."),
                           degraded=[Degraded.EXPLAINER_FALLBACK])
    return AgentResult.from_structured(res, res.value)
