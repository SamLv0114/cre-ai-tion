"""Brief Interpreter (LLM). Runs once per brief; its output is shared by the whole fan-out."""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain import extract
from ..domain.schemas import (
    DesignBrief, DesignIntent, Finish, Material, OpenQuestion, Priorities, Usage,
)
from ..llm.structured import call_structured
from ..prompts import render
from ..reliability.errors import StructuredOutputError
from .base import AgentContext, AgentResult

ROLE = "brief_interpreter"


def conservative_intent(brief: DesignBrief) -> DesignIntent:
    """Deterministic fallback: neutral priorities, numbers from the regex pass, a blocking question."""
    nc = extract.extract(brief.text)
    return DesignIntent(
        brief_id=brief.brief_id, summary=f"(fallback) {brief.text[:200]}",
        priorities=Priorities(cost=0.5, durability=0.5, aesthetics=0.5, sustainability=0.5), environment="mixed",
        allowed_materials=[Material.ABS, Material.PC_ABS, Material.RABS],
        preferred_finishes=[Finish.MATTE, Finish.TEXTURE_LIGHT], style_keywords=["neutral"],
        target_unit_cost_cny=nc.cost_cny, max_thickness_mm=nc.thickness_mm, max_mass_g=nc.mass_g,
        ip_rating=nc.ip or "none",
        open_questions=[OpenQuestion(question_id="QF", blocking=True,
                                     question="Interpretation fell back to defaults. Confirm the brief's priorities.",
                                     default_assumption="Balanced priorities; numeric limits taken from the text")],
    )


async def run(ctx: AgentContext, brief: DesignBrief) -> AgentResult[DesignIntent]:
    # near-identical brief seen before (same numbers too)? reuse its interpretation
    guard = extract.extract(brief.text).guard()
    if ctx.semantic_cache is not None:
        hit, sim = ctx.semantic_cache.lookup("intent", brief.text, guard)
        if hit is not None:
            intent = DesignIntent.model_validate({**hit, "brief_id": brief.brief_id})
            ctx.tracer.event("cache.hit", f"reused interpretation of a near-identical brief (cosine {sim:.3f}, "
                                          f"numeric guard matched)")
            return AgentResult(artifact=intent, usage=Usage(), model="cache", notes=[f"semantic cache {sim:.3f}"])

    messages, phash = render(ROLE, ctx.cfg.prompt_version, {"brief_id": brief.brief_id, "text": brief.text},
                             DesignIntent)
    # payload = same inputs as structured data; only the offline mock reads it
    payload = {"brief": brief.model_dump(mode="json"), "prompt_version": ctx.cfg.prompt_version}
    try:
        res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=DesignIntent, messages=messages,
                                    payload=payload, variant=f"{brief.brief_id}", max_repairs=ctx.cfg.max_repairs,
                                    temperature=0.1)
    except StructuredOutputError as exc:
        # no model gave a usable answer: fall back to neutral defaults and ask a human
        ctx.tracer.event("fallback", f"brief interpreter exhausted all routes ({exc}); using conservative intent")
        return AgentResult(artifact=conservative_intent(brief), usage=getattr(exc, "usage", Usage()),
                           degraded=[Degraded.INTERPRETER_FALLBACK])
    intent = res.value.model_copy(update={"brief_id": brief.brief_id})  # never trust ids from the model
    if ctx.semantic_cache is not None:
        ctx.semantic_cache.put("intent", brief.text, guard, intent.model_dump(mode="json"))
    return AgentResult.from_structured(res, intent, extra={"prompt_hash": phash})
