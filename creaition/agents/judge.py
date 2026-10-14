"""Quality Judge (LLM-as-judge) for concept quality and brief adherence.

Single pass, no revise loop: subjective scores are noisy, a judge-driven loop
invites reward hacking, and in a fan-out it is cheaper to generate-and-select
than to polish one variant. The judge only sees variants that already passed
the deterministic gates, so its tokens are spent where they inform a decision.
"""

from __future__ import annotations

from typing import Optional

from ..domain.drift import concept_drift
from ..domain.flags import Degraded
from ..domain.schemas import (
    ConceptSpec, CostReport, DesignContract, JudgeOutput, QualityReview, StructuralSpec, Usage,
)
from ..llm.structured import call_structured
from ..prompts import render
from ..reliability.budget import Budget
from ..reliability.errors import StructuredOutputError
from .base import AgentContext, AgentResult

ROLE = "quality_judge"


async def run(ctx: AgentContext, contract: DesignContract, concept: ConceptSpec, spec: StructuralSpec,
              cost: Optional[CostReport], budget: Optional[Budget], sample: int = 0) -> AgentResult[QualityReview]:
    inputs = {
        "brief_intent": contract.intent.model_dump(mode="json"), "concept": concept.model_dump(mode="json"),
        "structure": spec.model_dump(mode="json"),
        # the judge only sees the cost total; compliance and cost are already checked by code
        "cost_summary": {"total_cny": cost.total_cny, "ceiling_cny": cost.ceiling_cny} if cost else None,
        # found by comparing the two artifacts, so the judge doesn't have to trust the change log's wording
        "code_checked_changes": concept_drift(concept, spec),
    }
    messages, _ = render(ROLE, ctx.cfg.prompt_version, inputs, JudgeOutput)
    payload = {"contract": contract.model_dump(mode="json"), "concept": concept.model_dump(mode="json"),
               "structure": spec.model_dump(mode="json"), "cost": cost.model_dump(mode="json") if cost else None,
               "sample": sample}
    try:
        res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=JudgeOutput, messages=messages,
                                    payload=payload, variant=spec.variant_id, max_repairs=ctx.cfg.max_repairs,
                                    # sample > 0 is used by the eval to re-judge and measure how consistent the judge is
                                    budget=budget, temperature=0.0 if sample == 0 else 0.7, max_tokens=800)
    except StructuredOutputError as exc:
        ctx.tracer.event("fallback", "judge unavailable on every route; variant routed to human review")
        return AgentResult(artifact=None, usage=getattr(exc, "usage", Usage()), degraded=[Degraded.JUDGE_UNAVAILABLE])
    out = res.value
    # variant id comes from us, not from the judge's reply
    review = QualityReview(variant_id=spec.variant_id, scores=out.scores, verdict=out.verdict,
                           rationale=out.rationale, flags=out.flags, judge_model=res.model)
    return AgentResult.from_structured(res, review, notes=[f"overall {review.overall:.2f} ({review.verdict})"])
