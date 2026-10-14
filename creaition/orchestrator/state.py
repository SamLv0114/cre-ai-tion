"""Explicit, typed, immutable variant state and the pure reducer that advances it.

State is event-sourced: each step produces a StepOutcome (checkpointed), and
`apply(state, step, outcome)` is a pure function returning the next state. On
resume we replay checkpointed outcomes through the same reducer, so the
recovered state is exactly the state at the crash, without re-calling models.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..domain.flags import Degraded
from ..domain.schemas import (
    ConceptSpec, CostReport, DesignContract, DesignIntent, DFMReport, QualityReview, StructuralSpec, Usage,
    VariantStatus,
)
from ..reliability.idempotency import digest


# every kind of step a brief or a variant can take
class Step(str, Enum):
    INTERPRET_BRIEF = "interpret_brief"
    RESOLVE_CONTRACT = "resolve_contract"
    GENERATE_CONCEPT = "generate_concept"
    BUILD_STRUCTURE = "build_structure"
    CHECK_DFM = "check_dfm"
    AUTO_FIX = "auto_fix"
    REVISE = "revise"
    FALLBACK_BASELINE = "fallback_baseline"
    ESTIMATE_COST = "estimate_cost"
    REVIEW_QUALITY = "review_quality"
    FINALIZE = "finalize"
    DONE = "done"


# final verdict for a variant
class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VariantStatus
    reason: str
    caveats: list[str] = Field(default_factory=list)


# lets a stored step result be turned back into the right model on replay
ARTIFACT_TYPES: dict[str, type[BaseModel]] = {
    "ConceptSpec": ConceptSpec, "StructuralSpec": StructuralSpec, "DFMReport": DFMReport, "CostReport": CostReport,
    "QualityReview": QualityReview, "GateDecision": GateDecision, "DesignIntent": DesignIntent,
    "DesignContract": DesignContract,
}


class StepOutcome(BaseModel):
    """What gets checkpointed for one executed step."""

    model_config = ConfigDict(frozen=True)

    step: Step
    artifact_type: Optional[str] = None
    artifact: Optional[dict[str, Any]] = None
    usage: Usage = Field(default_factory=Usage)
    model: Optional[str] = None
    degraded: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: Optional[str] = None

    def typed(self) -> Optional[BaseModel]:
        if self.artifact is None or self.artifact_type is None:
            return None
        return ARTIFACT_TYPES[self.artifact_type].model_validate(self.artifact)


# everything we know about one variant; never edited in place, apply() returns a new one
class VariantState(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    variant_id: str
    brief_id: str
    direction: dict[str, Any]

    concept: Optional[ConceptSpec] = None
    structure: Optional[StructuralSpec] = None
    structure_source: Optional[str] = None  # llm | autofix | reviser | fallback
    dfm: Optional[DFMReport] = None
    cost: Optional[CostReport] = None
    review: Optional[QualityReview] = None
    judge_attempted: bool = False

    first_dfm_failures: Optional[list[str]] = None
    llm_revisions: int = 0
    autofixes: int = 0
    reviser_failures: int = 0
    step_errors: int = 0
    steps: int = 0
    history: tuple[str, ...] = ()
    spec_hashes: tuple[str, ...] = ()
    oscillation: bool = False
    fallback_used: bool = False
    budget_exhausted: bool = False
    degraded: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    status: VariantStatus = VariantStatus.PENDING
    reason: Optional[str] = None
    caveats: tuple[str, ...] = ()


def spec_fingerprint(spec: StructuralSpec) -> str:
    """Hash of the engineering content (ignores revision counter and change log) for oscillation detection."""
    return digest(spec.model_dump(mode="json", exclude={"revision", "change_log"}), length=16)


def apply(s: VariantState, step: Step, out: StepOutcome) -> VariantState:
    # always: count the step, add usage and any new fallback flags
    upd: dict[str, Any] = {
        "steps": s.steps + 1,
        "history": s.history + (step.value,),
        "usage": s.usage + out.usage,
        "degraded": s.degraded + tuple(d for d in out.degraded if d not in s.degraded),
    }
    if out.model:
        upd["models"] = s.models + (out.model,)
    if Degraded.BUDGET_EXHAUSTED in out.degraded:
        upd["budget_exhausted"] = True
    # the step crashed or timed out: count it and change nothing else
    if out.error and out.artifact is None:
        upd["step_errors"] = s.step_errors + 1
        if step == Step.REVISE:
            upd["llm_revisions"] = s.llm_revisions + 1
            upd["reviser_failures"] = s.reviser_failures + 1
        return s.model_copy(update=upd)

    art = out.typed()

    # a new structure invalidates the old DFM, cost and judge results
    def new_structure(spec: StructuralSpec, source: str) -> None:
        fp = spec_fingerprint(spec)
        upd.update(structure=spec, structure_source=source, dfm=None, cost=None, review=None,
                   judge_attempted=False, spec_hashes=s.spec_hashes + (fp,))
        # same spec as an earlier revision: the loop is going in circles
        if fp in s.spec_hashes:
            upd["oscillation"] = True

    if step == Step.GENERATE_CONCEPT:
        upd["concept"] = art
    elif step == Step.BUILD_STRUCTURE:
        new_structure(art, "fallback" if Degraded.STRUCTURAL_FALLBACK in out.degraded else "llm")
    elif step == Step.CHECK_DFM:
        upd["dfm"] = art
        # remember what the first draft got wrong (for learning and metrics)
        if s.first_dfm_failures is None:
            upd["first_dfm_failures"] = sorted({f.rule_id for f in art.failures})
    elif step == Step.AUTO_FIX:
        upd["autofixes"] = s.autofixes + 1
        new_structure(art, "autofix")
    elif step == Step.REVISE:
        upd["llm_revisions"] = s.llm_revisions + 1
        if art is None:
            upd["reviser_failures"] = s.reviser_failures + 1
        else:
            new_structure(art, "reviser")
    elif step == Step.FALLBACK_BASELINE:
        upd["fallback_used"] = True
        new_structure(art, "fallback")
    elif step == Step.ESTIMATE_COST:
        upd["cost"] = art
    elif step == Step.REVIEW_QUALITY:
        upd["review"] = art
        upd["judge_attempted"] = True
    elif step == Step.FINALIZE:
        upd.update(status=art.status, reason=art.reason, caveats=tuple(art.caveats))
    return s.model_copy(update=upd)
