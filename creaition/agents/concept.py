"""2D Concept Generator (LLM, fanned out over style directions)."""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain.schemas import (
    CMF, ConceptSpec, DesignContract, Dims2, Dims3, Face, IntentAnchor, Placement, Silhouette, Usage,
)
from ..llm.structured import call_structured
from ..prompts import render
from ..reliability.budget import Budget
from ..reliability.errors import StructuredOutputError
from ..state.memory import intent_tags
from .base import AgentContext, AgentResult

ROLE = "concept_generator"

# Deterministic style directions. Diversity across a fan-out is assigned by code
# (round-robin over directions), not left to sampling temperature: it is
# reproducible, cheap, and lets us measure mode collapse.
# one style direction per variant, handed out in order
DIRECTIONS: list[dict] = [
    {"name": "soft_pebble", "silhouette": "pebble", "intake_face": "front", "slim": True},
    {"name": "monolith_slab", "silhouette": "chamfered_slab", "intake_face": "top", "slim": True},
    {"name": "rugged_bumper", "silhouette": "bumpered", "intake_face": "front"},
    {"name": "capsule_handheld", "silhouette": "capsule", "intake_face": "back"},
    {"name": "architectural_rect", "silhouette": "rounded_rect", "intake_face": "top"},
    {"name": "tilted_desk_wedge", "silhouette": "chamfered_slab", "intake_face": "front"},
]


def directions_for(contract: DesignContract, n: int) -> list[dict]:
    """Order directions by fit to the brief's dominant priority, then cycle."""
    tags = intent_tags(contract.intent)
    order = list(DIRECTIONS)
    if "durability" in tags:
        # put the best-fitting direction first
        order.sort(key=lambda d: d["name"] != "rugged_bumper")
    elif "aesthetics" in tags:
        order.sort(key=lambda d: not d.get("slim", False))
    return [order[i % len(order)] for i in range(n)]


def template_concept(contract: DesignContract, direction: dict, variant_id: str) -> ConceptSpec:
    """Deterministic fallback concept: plain, safe, honest about being a fallback."""
    intent = contract.intent
    return ConceptSpec(
        variant_id=variant_id, brief_id=contract.brief_id, direction=direction["name"],
        name=f"Fallback {direction['name'].replace('_', ' ')}"[:60],
        form_language="Template concept generated without a model; neutral proportions.",
        silhouette=Silhouette(direction["silhouette"]), envelope_mm=Dims3(length=116, width=66, height=27),
        corner_radius_mm=8, display=Placement(face=Face.FRONT, x_pct=50, y_pct=35),
        display_window_mm=Dims2(width=44, height=20), intake=Placement(face=Face.FRONT, x_pct=50, y_pct=78),
        usb=Placement(face=Face.BOTTOM, x_pct=50, y_pct=50),
        cmf=CMF(material=contract.allowed_materials[0], finish=intent.preferred_finishes[0], color="neutral grey"),
        intent_anchors=[
            IntentAnchor(anchor_id="IA1", statement="Neutral rounded form", category="form"),
            IntentAnchor(anchor_id="IA2", statement="Display upper-centre on the front face", category="usability"),
        ],
        rationale="Deterministic template used because no model returned a valid concept.",
    )


async def run(ctx: AgentContext, contract: DesignContract, direction: dict, variant_id: str,
              budget: Budget) -> AgentResult[ConceptSpec]:
    # lessons from earlier runs that apply to this kind of brief
    lessons = ctx.memory.for_agent(ROLE, intent_tags(contract.intent))
    # shared contract first, variant-specific fields last -> longest common prompt prefix across the fan-out
    inputs = {"contract": contract.model_dump(mode="json"), "direction": direction, "variant_id": variant_id}
    messages, phash = render(ROLE, ctx.cfg.prompt_version, inputs, ConceptSpec, lessons)
    payload = {**inputs, "prompt_version": ctx.cfg.prompt_version,
               "lessons": [l.model_dump() for l in lessons]}
    try:
        res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=ConceptSpec, messages=messages,
                                    payload=payload, variant=variant_id, max_repairs=ctx.cfg.max_repairs,
                                    # higher temperature on purpose: we want variety here
                                    budget=budget, temperature=0.8)
    except StructuredOutputError as exc:
        ctx.tracer.event("fallback", f"concept generator failed on every route; deterministic template ({exc})")
        return AgentResult(artifact=template_concept(contract, direction, variant_id),
                           usage=getattr(exc, "usage", Usage()), degraded=[Degraded.CONCEPT_FALLBACK])
    # ids come from us, not the model
    concept = res.value.model_copy(update={"variant_id": variant_id, "brief_id": contract.brief_id,
                                           "direction": direction["name"]})
    return AgentResult.from_structured(res, concept, extra={"prompt_hash": phash})
