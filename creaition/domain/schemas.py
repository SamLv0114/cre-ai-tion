"""Data passed between agents. All models are frozen: agents return new objects, nothing edits shared state.

Schemas only check shape and physical sanity. Engineering limits (wall 1.5-3.0 mm and so on) live in
dfm_rules, so a spec can be valid here and still fail DFM. Derived numbers (mass, open area, cost) are
always recomputed by code, never taken from a model.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator


def Prose(max_len: int):  # noqa: N802 - reads like a type
    """Free-text field: over-long prose is truncated by code instead of costing a repair call.

    Live finding: deepseek-flash wrote 120+ character colour descriptions even after two repair prompts. A
    closed-form fix (truncate) belongs in code; schema repair is reserved for real violations (wrong enum, missing data).
    """
    def cut(v):
        return v[: max_len - 1] + "…" if isinstance(v, str) and len(v) > max_len else v
    return Annotated[str, BeforeValidator(cut), Field(min_length=2, max_length=max_len)]


class Artifact(BaseModel):
    # Prose fields carry generous sanity caps only; engineering limits live in the DFM rules.
    # extra="ignore": live models often add harmless keys; failing on them would
    # burn repair attempts for no quality gain.
    model_config = ConfigDict(frozen=True, extra="ignore", use_enum_values=False)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
# the three resins the brief allows
class Material(str, Enum):
    ABS = "ABS"
    PC_ABS = "PC-ABS"
    RABS = "rABS"  # recycled (post-consumer) ABS


# textured finishes need more draft (2 deg vs 1 deg)
class Finish(str, Enum):
    GLOSS = "smooth_gloss"
    MATTE = "smooth_matte"
    TEXTURE_LIGHT = "textured_light"
    TEXTURE_HEAVY = "textured_heavy"

    @property
    def textured(self) -> bool:
        return self in (Finish.TEXTURE_LIGHT, Finish.TEXTURE_HEAVY)


class Silhouette(str, Enum):
    ROUNDED_RECT = "rounded_rect"
    PEBBLE = "pebble"
    CHAMFERED_SLAB = "chamfered_slab"
    CAPSULE = "capsule"
    BUMPERED = "bumpered"


# faces of the outer box, used to place the display, intake and USB
class Face(str, Enum):
    FRONT = "front"
    BACK = "back"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


# post-moulding decoration; each one adds per-unit cost
class SecondaryOp(str, Enum):
    PAINT = "paint"
    SOFT_TOUCH = "soft_touch"
    PAD_PRINT = "pad_print"
    LASER_ETCH = "laser_etch"
    IML = "iml"
    TPE_OVERMOLD = "tpe_overmold"


class Closure(str, Enum):
    SCREWS = "screws"
    SNAP_FITS = "snap_fits"
    HYBRID = "screws_and_snaps"


class Sealing(str, Enum):
    NONE = "none"
    GASKET = "gasket"
    GASKET_MEMBRANE = "gasket_and_membrane_vent"


# how the mould releases an undercut; sliders and lifters add tooling cost
class UndercutMechanism(str, Enum):
    SLIDER = "slider"
    LIFTER = "lifter"
    BUMP_OFF = "bump_off"


# where a variant ends up; only PRODUCTION_READY needs a recorded approval
class VariantStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"  # passed every automated gate, awaits a human
    PRODUCTION_READY = "production_ready"  # approved by a human (or auto policy in eval)
    NEEDS_HUMAN = "needs_human"  # passed checks only via degraded paths / unresolved conflicts
    REJECTED = "rejected"  # failed gates after bounded revision
    FAILED = "failed"  # infrastructure failure (isolated, did not cascade)
    SKIPPED = "skipped"  # not run (run budget or run-health breaker)

    @property
    def terminal(self) -> bool:
        return self not in (VariantStatus.PENDING, VariantStatus.RUNNING)


# --------------------------------------------------------------------------
# Small value objects
# --------------------------------------------------------------------------
class Dims2(Artifact):
    width: float = Field(gt=0, le=200)
    height: float = Field(gt=0, le=200)


class Dims3(Artifact):
    length: float = Field(gt=0, le=1000)
    width: float = Field(gt=0, le=1000)
    height: float = Field(gt=0, le=1000)


# tokens, money and time spent; added up per step, per variant and per run
class Usage(Artifact):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    calls: int = 0
    latency_ms: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_cny=round(self.cost_cny + other.cost_cny, 6),
            calls=self.calls + other.calls,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------
# Brief and interpretation
# --------------------------------------------------------------------------
class DesignBrief(Artifact):
    brief_id: str
    title: str
    text: str


# 0..1 weight per concern; the highest one is the brief's 'dominant' priority
class Priorities(Artifact):
    cost: float = Field(ge=0, le=1)
    durability: float = Field(ge=0, le=1)
    aesthetics: float = Field(ge=0, le=1)
    sustainability: float = Field(ge=0, le=1)


# requirements that can't all be met; 'hard' means physically or economically impossible
class ConflictNote(Artifact):
    conflict_id: str
    description: str = Field(min_length=5)
    requirements: list[str] = Field(default_factory=list)
    severity: Literal["hard", "soft"] = "soft"
    proposed_resolution: str = Field(min_length=3)
    source: Literal["llm", "rule"] = "llm"


# something to ask the designer, with the default we use if nobody answers
class OpenQuestion(Artifact):
    question_id: str
    question: str = Field(min_length=5)
    default_assumption: str = Field(min_length=3)
    blocking: bool = False


class DesignIntent(Artifact):
    """Output of the Brief Interpreter (LLM)."""

    brief_id: str
    summary: Prose(2000) = Field(min_length=10)
    priorities: Priorities
    environment: Literal["indoor", "outdoor", "mixed"]
    allowed_materials: list[Material] = Field(min_length=1)
    preferred_finishes: list[Finish] = Field(min_length=1)
    style_keywords: list[str] = Field(min_length=1, max_length=8)
    target_unit_cost_cny: Optional[float] = Field(default=None, gt=0, le=200)
    max_thickness_mm: Optional[float] = Field(default=None, gt=0, le=200)
    max_mass_g: Optional[float] = Field(default=None, gt=0, le=2000)
    ip_rating: Literal["none", "IP54", "IP65", "IP67"] = "none"
    drop_height_m: Optional[float] = Field(default=None, ge=0, le=10)
    must_haves: list[str] = Field(default_factory=list)
    conflicts: list[ConflictNote] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class Relaxation(Artifact):
    """A constraint the resolver proposes to relax because the brief is infeasible as written."""

    field: Literal["cost_ceiling_cny", "max_height_mm", "max_mass_g"]
    strict_value: float
    relaxed_value: float
    reason: str
    accepted: bool = True  # batch mode adopts the default; the Ask gate lets a human refuse it


class DesignContract(Artifact):
    """Deterministic merge of the interpreted intent with the hard constraints.

    Produced by plain code (ConstraintResolver), shared read-only by every
    variant in the fan-out.
    """

    brief_id: str
    intent: DesignIntent
    cost_ceiling_cny: float
    max_envelope_mm: Dims3
    max_mass_g: float
    allowed_materials: list[Material]
    ip_rating: Literal["none", "IP54", "IP65", "IP67"]
    floor_cost_cny: float
    conflicts: list[ConflictNote] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    relaxations: list[Relaxation] = Field(default_factory=list)
    # things no relaxation can fix, because the fix would break a platform limit (e.g. cost floor > CNY 18)
    platform_blockers: list[str] = Field(default_factory=list)
    # filled by apply_decisions: platform blockers + any relaxation the designer refused
    infeasible_reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    requires_human_resolution: bool = False

    @property
    def conflict_detected(self) -> bool:
        return any(c.severity == "hard" for c in self.conflicts)

    @property
    def rule_conflict_detected(self) -> bool:
        """A hard conflict proven by code (arithmetic or an out-of-platform request), not just a model's label."""
        return any(c.severity == "hard" and c.source == "rule" for c in self.conflicts)

    @property
    def infeasible(self) -> bool:
        """No variant can pass: don't fan out, report why instead."""
        return bool(self.infeasible_reasons)


# --------------------------------------------------------------------------
# 2D concept
# --------------------------------------------------------------------------
# where something sits on a face, as % across and % down
class Placement(Artifact):
    face: Face
    x_pct: float = Field(ge=0, le=100, description="Centre position along the face width, percent")
    y_pct: float = Field(ge=0, le=100, description="Centre position along the face height, percent")


class IntentAnchor(Artifact):
    """A named piece of design intent that downstream stages must preserve."""

    anchor_id: str = Field(pattern=r"^IA\d{1,2}$")
    statement: Prose(500) = Field(min_length=5)
    category: Literal["form", "usability", "cmf", "durability", "sustainability", "cost"]
    must_preserve: bool = True


# colour, material, finish
class CMF(Artifact):
    material: Material
    finish: Finish
    color: Prose(120)
    secondary_ops: list[SecondaryOp] = Field(default_factory=list)


class ConceptSpec(Artifact):
    """2D concept specification (front/side elevation level of detail)."""

    variant_id: str
    brief_id: str
    direction: str
    name: Prose(120) = Field(min_length=3)
    form_language: Prose(2000) = Field(min_length=10)
    silhouette: Silhouette
    envelope_mm: Dims3
    corner_radius_mm: float = Field(ge=0, le=40)
    display: Placement
    display_window_mm: Dims2
    intake: Placement
    usb: Placement
    cmf: CMF
    intent_anchors: list[IntentAnchor] = Field(min_length=2, max_length=8)
    rationale: Prose(3000) = Field(min_length=10)

    @field_validator("intent_anchors")
    @classmethod
    def _unique_anchor_ids(cls, v: list[IntentAnchor]) -> list[IntentAnchor]:
        ids = [a.anchor_id for a in v]
        if len(ids) != len(set(ids)):
            raise ValueError("intent anchor ids must be unique")
        return v


# --------------------------------------------------------------------------
# 3D-ready structural spec
# --------------------------------------------------------------------------
# draft angle for one group of faces
class FaceDraft(Artifact):
    face: str = Field(min_length=2, max_length=40)
    draft_deg: float = Field(ge=0, le=15)
    textured: bool = False


class Rib(Artifact):
    rib_id: str
    thickness_mm: float = Field(gt=0, le=10)
    height_mm: float = Field(gt=0, le=30)
    length_mm: float = Field(gt=0, le=130)
    count: int = Field(ge=1, le=40)


# screw boss: a post that takes a screw or insert
class Boss(Artifact):
    boss_id: str
    outer_d_mm: float = Field(gt=0, le=20)
    inner_d_mm: float = Field(gt=0, le=15)
    height_mm: float = Field(gt=0, le=30)
    count: int = Field(ge=1, le=16)

    @model_validator(mode="after")
    def _id_lt_od(self) -> "Boss":
        if self.inner_d_mm >= self.outer_d_mm:
            raise ValueError("boss inner diameter must be smaller than outer diameter")
        return self


class Undercut(Artifact):
    undercut_id: str
    feature: Prose(300) = Field(min_length=3)
    # Optional on purpose: the DFM rule (not the schema) fails undeclared undercuts.
    mechanism: Optional[UndercutMechanism] = None
    side_action_cost_cny: Optional[float] = Field(default=None, ge=0, le=500_000)


# one moulded part (front shell, rear shell or bumper)
class PartSpec(Artifact):
    part_id: str
    role: Literal["front_housing", "rear_housing", "bumper"]
    material: Material
    finish: Finish
    nominal_wall_mm: float = Field(gt=0.2, le=10)
    min_wall_mm: float = Field(gt=0.2, le=10)
    max_wall_mm: float = Field(gt=0.2, le=10)
    inner_radius_mm: float = Field(ge=0, le=20)
    drafts: list[FaceDraft] = Field(min_length=1)
    ribs: list[Rib] = Field(default_factory=list)
    bosses: list[Boss] = Field(default_factory=list)
    undercuts: list[Undercut] = Field(default_factory=list)
    secondary_ops: list[SecondaryOp] = Field(default_factory=list)

    @model_validator(mode="after")
    def _wall_order(self) -> "PartSpec":
        if not (self.min_wall_mm <= self.nominal_wall_mm <= self.max_wall_mm):
            raise ValueError("require min_wall_mm <= nominal_wall_mm <= max_wall_mm")
        return self


# intake grille: a zone filled with parallel slots
class VentSpec(Artifact):
    face: Face
    zone_w_mm: float = Field(gt=0, le=120)
    zone_h_mm: float = Field(gt=0, le=120)
    slot_count: int = Field(ge=0, le=60)
    slot_w_mm: float = Field(gt=0, le=10)
    slot_l_mm: float = Field(gt=0, le=120)


# which spec features carry one concept intent anchor
class IntentTrace(Artifact):
    anchor_id: str = Field(pattern=r"^IA\d{1,2}$")
    realized_by: list[str] = Field(min_length=1)
    note: str = ""


class StructuralSpec(Artifact):
    """3D-ready structural specification: enough to start CAD and a tooling quote."""

    variant_id: str
    brief_id: str
    concept_name: str
    revision: int = Field(ge=0, le=50)
    envelope_mm: Dims3
    parting: Literal["front_back", "top_bottom"]
    parts: list[PartSpec] = Field(min_length=2, max_length=3)
    display_window_mm: Dims2
    vent: VentSpec
    usb_opening_mm: Dims2
    internal_layout: Literal["stacked"] = "stacked"
    closure: Closure
    screw_count: int = Field(ge=0, le=12)
    sealing: Sealing
    intent_trace: list[IntentTrace] = Field(default_factory=list)
    change_log: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_parts(self) -> "StructuralSpec":
        ids = [p.part_id for p in self.parts]
        if len(ids) != len(set(ids)):
            raise ValueError("part ids must be unique")
        roles = {p.role for p in self.parts}
        if not {"front_housing", "rear_housing"} <= roles:
            raise ValueError("a housing needs at least a front_housing and a rear_housing part")
        return self

    # every id an intent trace is allowed to point at
    def feature_ids(self) -> set[str]:
        ids: set[str] = {"envelope", "display_window", "vent", "usb_opening", "closure", "sealing", "parting"}
        for p in self.parts:
            ids.add(p.part_id)
            ids.update(r.rib_id for r in p.ribs)
            ids.update(b.boss_id for b in p.bosses)
            ids.update(u.undercut_id for u in p.undercuts)
            ids.update(f"{p.part_id}.{op.value}" for op in p.secondary_ops)
            ids.add(f"{p.part_id}.finish")
            ids.add(f"{p.part_id}.material")
        return ids


# --------------------------------------------------------------------------
# Critique artifacts
# --------------------------------------------------------------------------
# outcome of one DFM rule
class RuleResult(Artifact):
    rule_id: str
    description: str
    status: Literal["pass", "fail", "warn"]
    severity: Literal["hard", "advisory"] = "hard"
    part_id: Optional[str] = None
    measured: Optional[float] = None
    limit: Optional[str] = None
    fix: Literal["auto", "design", "none"] = "none"
    hint: str = ""


class DFMReport(Artifact):
    variant_id: str
    revision: int
    results: list[RuleResult]
    engine: Literal["remote_full", "local_baseline"]
    confidence: Literal["full", "reduced"]
    headroom: float = Field(description="Mean normalised slack over numeric rules; tracks silent regressions")
    housing_mass_g: float
    product_mass_g: float
    open_area_ratio: float
    usb_clearance_mm: float

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == "fail" and r.severity == "hard"]

    @property
    def passed(self) -> bool:
        return not self.failures

    # true when every hard failure has a closed-form fix (no LLM needed)
    @property
    def only_auto_fixable(self) -> bool:
        return bool(self.failures) and all(r.fix == "auto" for r in self.failures)


# one line of the cost breakdown
class CostLine(Artifact):
    item: str
    cny: float


class CostReport(Artifact):
    variant_id: str
    revision: int
    lines: list[CostLine]
    total_cny: float
    ceiling_cny: float
    within_budget: bool
    tooling_total_cny: float
    housing_mass_g: float
    price_source: Literal["live", "snapshot"]
    quote_source: Literal["live", "rate_table"]

    @property
    def headroom_cny(self) -> float:
        return round(self.ceiling_cny - self.total_cny, 2)


# judge rubric, each 1-5
class QualityScores(Artifact):
    brief_adherence: int = Field(ge=1, le=5)
    form_coherence: int = Field(ge=1, le=5)
    cmf_fit: int = Field(ge=1, le=5)
    usability: int = Field(ge=1, le=5)
    intent_preservation: int = Field(ge=1, le=5)


class QualityReview(Artifact):
    variant_id: str
    scores: QualityScores
    verdict: Literal["accept", "weak", "reject"]
    rationale: Prose(3000) = Field(min_length=10)
    flags: list[str] = Field(default_factory=list)
    judge_model: str = ""

    @property
    def overall(self) -> float:
        s = self.scores
        return round(
            # brief adherence counts double
            (s.brief_adherence * 2 + s.form_coherence + s.cmf_fit + s.usability + s.intent_preservation) / 6, 3
        )


# --------------------------------------------------------------------------
# Reviser tool-loop protocol (LLM-chosen actions, validated as a discriminated union)
# --------------------------------------------------------------------------
class CostWhatIfArgs(Artifact):
    """Arguments the reviser may pass to cost_what_if (validated like any other model output)."""

    material: Optional[Material] = None
    drop_secondary_ops: list[SecondaryOp] = Field(default_factory=list)
    remove_bumper: bool = False


class MaterialsLookupArgs(Artifact):
    material: Material


TOOL_ARGS = {"cost_what_if": CostWhatIfArgs, "materials_lookup": MaterialsLookupArgs}


class ToolCallAction(Artifact):
    action: Literal["tool_call"]
    tool: Literal["materials_lookup", "cost_what_if"]
    args: dict
    reason: str = ""


class ReviseFinalAction(Artifact):
    action: Literal["final"]
    structure: StructuralSpec
    changes: list[str] = Field(min_length=1)


# wrapper so the model's reply is parsed as either a tool call or a final answer
class ReviserAction(Artifact):
    step: ToolCallAction | ReviseFinalAction = Field(discriminator="action")


class JudgeOutput(Artifact):
    """Raw judge output (variant id is attached by code, not trusted from the model)."""

    scores: QualityScores
    verdict: Literal["accept", "weak", "reject"]
    rationale: Prose(3000) = Field(min_length=10)
    flags: list[str] = Field(default_factory=list)


# reply from the Ask/explain feature
class Answer(Artifact):
    answer: str = Field(min_length=3)
    cited_variants: list[str] = Field(default_factory=list)
