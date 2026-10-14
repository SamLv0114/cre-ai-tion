"""3D-Readiness Modeler (LLM). Converts a 2D concept into a structural spec.

Intent hand-off: the concept names its intent anchors (IA1..), the modeler must
map each one to concrete feature ids in `intent_trace`, and the DFM critic's
INTENT_TRACE_COMPLETE rule verifies the mapping against the spec's real feature
ids. "Lost design intent" becomes a failing check rather than a vibe.
"""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain.baseline import baseline_structure
from ..domain.schemas import ConceptSpec, DesignContract, StructuralSpec, Usage
from ..llm.structured import call_structured
from ..prompts import render
from ..reliability.budget import Budget
from ..reliability.errors import StructuredOutputError
from ..state.memory import intent_tags
from .base import AgentContext, AgentResult

ROLE = "structural_modeler"


async def run(ctx: AgentContext, contract: DesignContract, concept: ConceptSpec,
              budget: Budget) -> AgentResult[StructuralSpec]:
    lessons = ctx.memory.for_agent(ROLE, intent_tags(contract.intent))
    inputs = {"contract": contract.model_dump(mode="json"), "concept": concept.model_dump(mode="json")}
    messages, phash = render(ROLE, ctx.cfg.prompt_version, inputs, StructuralSpec, lessons)
    payload = {**inputs, "prompt_version": ctx.cfg.prompt_version, "lessons": [l.model_dump() for l in lessons]}
    try:
        res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=StructuralSpec, messages=messages,
                                    payload=payload, variant=concept.variant_id, max_repairs=ctx.cfg.max_repairs,
                                    # low temperature: this step is about precise numbers
                                    budget=budget, temperature=0.2)
    except StructuredOutputError as exc:
        # no usable answer: use the safe template structure (marked degraded)
        ctx.tracer.event("fallback", "structural modeler failed on every route; deterministic baseline structure")
        return AgentResult(artifact=baseline_structure(concept, contract, 0, "modeler unavailable"),
                           usage=getattr(exc, "usage", Usage()), degraded=[Degraded.STRUCTURAL_FALLBACK])
    spec = res.value.model_copy(update={"variant_id": concept.variant_id, "brief_id": concept.brief_id,
                                        "revision": 0})
    return AgentResult.from_structured(res, spec, extra={"prompt_hash": phash})
