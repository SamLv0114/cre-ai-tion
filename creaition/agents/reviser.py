"""Design reviser: the one LLM step inside the critic-and-revise loop.

The DFM and cost checks (code) say what is wrong; the reviser chooses the trade-off. It may price options
with a tool first (at most `max_reviser_tool_calls`), then returns a full revised spec. Tool calls and
final answers are told apart by schema validation, not by reading the text.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import ValidationError

from ..domain.flags import Degraded
from ..domain.schemas import (
    TOOL_ARGS, ConceptSpec, CostReport, DesignContract, DFMReport, ReviseFinalAction, ReviserAction, StructuralSpec,
    Usage,
)
from ..llm.structured import call_structured, feedback
from ..prompts import render
from ..reliability.budget import Budget
from ..reliability.errors import StructuredOutputError
from ..state.memory import intent_tags
from ..tools.services import cost_what_if, materials_info
from .base import AgentContext, AgentResult

ROLE = "reviser"


async def run(ctx: AgentContext, contract: DesignContract, concept: ConceptSpec, spec: StructuralSpec,
              dfm: Optional[DFMReport], cost: Optional[CostReport], budget: Budget) -> AgentResult[StructuralSpec]:
    # only the failing rules go to the model, not the whole report
    failures = [f.model_dump(mode="json") for f in (dfm.failures if dfm else [])]
    lessons = ctx.memory.for_agent(ROLE, intent_tags(contract.intent))
    inputs = {
        "contract": contract.model_dump(mode="json"), "concept": concept.model_dump(mode="json"),
        "structure": spec.model_dump(mode="json"), "failures": failures,
        "cost": cost.model_dump(mode="json") if cost else None,
    }
    tool_results: list[dict] = []
    usage = Usage()
    models: list[str] = []
    notes: list[str] = []
    max_tools = ctx.cfg.max_reviser_tool_calls
    # each turn the model either calls a tool or returns the revised spec; capped at max_tools calls
    for turn in range(max_tools + 1):
        payload = {**inputs, "tool_results": tool_results, "prompt_version": ctx.cfg.prompt_version,
                   "lessons": [l.model_dump() for l in lessons]}
        messages, _ = render(ROLE, ctx.cfg.prompt_version, inputs, ReviserAction, lessons, max_tools=max_tools)
        # replay earlier tool calls and their results so the model sees what it already tried
        for tr in tool_results:
            messages.append({"role": "assistant", "content": json.dumps({"step": tr["call"]})})
            messages.append({"role": "user", "content": "Tool result: " + json.dumps(tr["result"])})
        if turn == max_tools:
            # last turn: no more tools allowed
            messages.append({"role": "user", "content": "Tool budget exhausted. Return the final action now."})
        try:
            res = await call_structured(ctx.router, ctx.tracer, role=ROLE, schema=ReviserAction, messages=messages,
                                        payload=payload, variant=spec.variant_id, max_repairs=ctx.cfg.max_repairs,
                                        budget=budget, temperature=0.2, max_tokens=4000)
        except StructuredOutputError as exc:
            ctx.tracer.event("fallback", f"reviser produced no valid action ({exc}); loop will fall back")
            return AgentResult(artifact=None, usage=usage + getattr(exc, "usage", Usage()),
                               degraded=[Degraded.REVISER_FAILED], notes=notes)
        usage = usage + res.usage
        models.append(res.model)
        step = res.value.step
        # final answer: keep our ids, bump the revision, append to the change log
        if isinstance(step, ReviseFinalAction):
            revised = step.structure.model_copy(update={
                "variant_id": spec.variant_id, "brief_id": spec.brief_id, "revision": spec.revision + 1,
                "change_log": list(spec.change_log) + [c for c in step.changes if c not in spec.change_log],
            })
            notes.append("; ".join(step.changes)[:300])
            return AgentResult(artifact=revised, usage=usage, model=models[-1], notes=notes,
                               extra={"tool_calls": len(tool_results)})
        if turn == max_tools:
            break
        # Tool arguments are model output too: validate them, and hand errors back to the model as a tool result
        # (it can correct itself within the tool budget) instead of letting a bad argument crash the step.
        # Live finding: deepseek-flash sent {"drop_secondary_ops": true}, which used to raise a TypeError.
        try:
            args = TOOL_ARGS[step.tool].model_validate(step.args)
            if step.tool == "cost_what_if":
                result = cost_what_if(spec, contract, args.model_dump(mode="json"))
            else:
                result = materials_info(args.material.value)
        except ValidationError as exc:
            result = {"error": "invalid tool arguments", "details": feedback(exc),
                      "expected_schema": TOOL_ARGS[step.tool].model_json_schema()}
            ctx.tracer.event("schema.invalid", f"reviser sent invalid {step.tool} args ({feedback(exc)[:100]}); "
                                               f"returned the error to the model")
        ctx.tracer.event("tool", f"reviser called {step.tool}({json.dumps(step.args)[:90]}) -> "
                                 f"{json.dumps(result)[:110]}", console=True)
        tool_results.append({"call": step.model_dump(mode="json"), "result": result})
    ctx.tracer.event("guard", f"reviser kept calling tools past the cap ({max_tools}); treated as failed turn")
    return AgentResult(artifact=None, usage=usage, degraded=[Degraded.REVISER_TOOL_CAP], notes=notes)
