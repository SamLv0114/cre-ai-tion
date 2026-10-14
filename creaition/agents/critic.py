"""DFM Critic and AutoFixer (deterministic).

The critic calls two tools (materials_lookup for densities, dfm_validate for the
rule set). The AutoFixer applies closed-form fixes for failures tagged
fix="auto" - e.g. rib thickness := 0.5 x wall - with zero tokens. Only failures
that need a trade-off (fix="design") are escalated to the LLM Reviser.
"""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain import constants as C
from ..domain import dfm_rules
from ..domain.schemas import (
    ConceptSpec, DesignContract, DFMReport, Dims2, FaceDraft, Sealing, StructuralSpec, UndercutMechanism,
)
from ..domain import catalog as K
from ..domain import geometry as G
from .base import AgentContext, AgentResult


async def run_dfm(ctx: AgentContext, contract: DesignContract, concept: ConceptSpec,
                  spec: StructuralSpec) -> AgentResult[DFMReport]:
    variant = spec.variant_id
    # tool 1: material densities (falls back to a snapshot if the service is down)
    records, mat_degraded = await ctx.tools.materials({p.material for p in spec.parts}, variant)
    densities = {m: r.density_g_cm3 for m, r in records.items()}
    anchors = [a.anchor_id for a in concept.intent_anchors if a.must_preserve]
    # tool 2: run the rules (falls back to the local rule set)
    report, dfm_degraded = await ctx.tools.dfm_validate(spec, contract, densities, anchors, variant)
    degraded = ([Degraded.MATERIALS_SNAPSHOT] if mat_degraded else []) + ([Degraded.DFM_LOCAL] if dfm_degraded else [])
    fails = ", ".join(sorted({f.rule_id for f in report.failures})) or "none"
    return AgentResult(artifact=report, degraded=degraded, notes=[f"hard failures: {fails}"])


def autofix(spec: StructuralSpec, report: DFMReport) -> AgentResult[StructuralSpec]:
    # only touch failures that have a formula fix; design trade-offs go to the reviser
    rules = {f.rule_id for f in report.failures if f.fix == "auto"}
    changes: list[str] = []
    parts = []
    for p in spec.parts:
        upd: dict = {}
        wall = p.nominal_wall_mm
        # pull the wall back inside 1.5-3.0 with a little margin
        if "WALL_RANGE" in rules:
            wall = min(max(wall, C.WALL_MIN_MM + 0.1), C.WALL_MAX_MM - 0.2)
            upd.update(nominal_wall_mm=wall,
                       min_wall_mm=min(max(p.min_wall_mm, C.WALL_MIN_MM), wall),
                       max_wall_mm=max(min(p.max_wall_mm, C.WALL_MAX_MM), wall))
        # ribs and radius follow the wall, so recompute them if the wall moved
        if "RIB_RATIO" in rules or "WALL_RANGE" in rules:
            upd["ribs"] = [r.model_copy(update={"thickness_mm": round(0.5 * wall, 2)}) for r in p.ribs]
        if "INNER_RADIUS" in rules or "WALL_RANGE" in rules:
            upd["inner_radius_mm"] = max(p.inner_radius_mm, round(C.INNER_RADIUS_RATIO_MIN * wall + 0.1, 2))
        drafts = list(p.drafts)
        # add the missing side of the wall at a safe draft
        if "DRAFT_COVERAGE" in rules:
            if not any(dfm_rules.OUTER_FACE.search(d.face) for d in drafts):
                drafts.insert(0, FaceDraft(face="outer_sides", draft_deg=C.DRAFT_MIN_SMOOTH_DEG + 0.5,
                                           textured=p.finish.textured))
            if not any(dfm_rules.INNER_FACE.search(d.face) for d in drafts):
                drafts.append(FaceDraft(face="inner_sides", draft_deg=C.DRAFT_MIN_SMOOTH_DEG + 0.5))
        if "TEXTURE_DECLARED" in rules and p.finish.textured and not any(d.textured for d in drafts):
            drafts[0] = drafts[0].model_copy(update={"textured": True})  # first listed face is the cosmetic one
        if rules & {"DRAFT_MIN", "TEXTURE_DECLARED", "DRAFT_COVERAGE"}:
            upd["drafts"] = [d.model_copy(update={"draft_deg": max(
                d.draft_deg, C.DRAFT_MIN_TEXTURED_DEG + 0.5 if d.textured else C.DRAFT_MIN_SMOOTH_DEG + 0.5)})
                for d in drafts]
        # declare a lifter and its standard tooling cost
        if "UNDERCUT_DECLARED" in rules:
            upd["undercuts"] = [u.model_copy(update={
                "mechanism": u.mechanism or UndercutMechanism.LIFTER,
                "side_action_cost_cny": u.side_action_cost_cny if u.side_action_cost_cny is not None
                else K.SIDE_ACTION_CNY[(u.mechanism or UndercutMechanism.LIFTER).value]}) for u in p.undercuts]
        parts.append(p.model_copy(update=upd) if upd else p)
    spec_upd: dict = {"parts": parts}
    if rules & {"VENT_OPEN_AREA", "VENT_ZONE_MIN", "VENT_SLOTS_FIT", "VENT_FITS_FACE"}:
        # Keep the designer's zone proportions (a wide, short louver stays wide and short); only grow each side to
        # the 18 mm minimum (+2) and clamp to the face it sits on. Slots run along the zone's long side.
        a, b = G.face_dims(spec, spec.vent.face)
        long_side = min(max(spec.vent.zone_w_mm, spec.vent.zone_h_mm, C.INTAKE_MIN_W + 2), a - 4)
        short_side = min(max(min(spec.vent.zone_w_mm, spec.vent.zone_h_mm), C.INTAKE_MIN_H + 2), b - 4)
        slot_w, bar = 1.2, C.VENT_MIN_BAR_MM
        count = int((short_side - bar) // (slot_w + bar))
        spec_upd["vent"] = spec.vent.model_copy(update={"zone_w_mm": long_side, "zone_h_mm": short_side,
                                                        "slot_count": count, "slot_w_mm": slot_w,
                                                        "slot_l_mm": long_side - 2 * bar - 2})
    # receptacle + 0.5 mm each side, plus 0.5 mm margin
    if rules & {"USB_OPENING_MIN", "USB_CLEARANCE"}:
        spec_upd["usb_opening_mm"] = Dims2(width=max(C.USB_OPENING_MIN_W, C.USB_RECEPTACLE_W + 2 * C.USB_CLEARANCE_MIN_MM) + 0.5,
                                           height=max(C.USB_OPENING_MIN_H, C.USB_RECEPTACLE_H + 2 * C.USB_CLEARANCE_MIN_MM) + 0.5)
    if "DISPLAY_WINDOW_MIN" in rules:
        spec_upd["display_window_mm"] = Dims2(width=max(spec.display_window_mm.width, C.DISPLAY_MIN_W),
                                              height=max(spec.display_window_mm.height, C.DISPLAY_MIN_H))
    if "SEALING_FOR_IP" in rules:
        spec_upd["sealing"] = Sealing.GASKET_MEMBRANE
    changes = [f"autofix {r}" for r in sorted(rules)]
    # a fixed spec is a new revision, so the planner re-checks it
    spec_upd["revision"] = spec.revision + 1
    spec_upd["change_log"] = list(spec.change_log) + changes
    return AgentResult(artifact=spec.model_copy(update=spec_upd), notes=changes)
