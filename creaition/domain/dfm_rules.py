"""DFM rule engine (the critic). Plain code: every rule in the brief is a simple inequality.

Each failure is tagged fix="auto" (a formula fix exists; the AutoFixer applies it) or fix="design"
(it needs a trade-off; it goes to the LLM reviser).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from . import constants as C
from . import geometry as G
from .schemas import Closure, DesignContract, DFMReport, RuleResult, Sealing, StructuralSpec

# face names a model may use for the two sides of a moulded wall
OUTER_FACE = re.compile(r"outer|exterior|outside|cosmetic|a[-_ ]?side", re.I)
INNER_FACE = re.compile(r"inner|interior|inside|core|b[-_ ]?side", re.I)


# keep each slack value in [-1, 1] so one huge margin can't dominate the average
def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def evaluate(
    spec: StructuralSpec,
    contract: DesignContract,
    densities: dict,
    must_preserve_anchor_ids: Iterable[str] = (),
    *,
    full: bool = True,
) -> DFMReport:
    """Run the rule set. `full=False` is the local fallback (required rules only)."""
    results: list[RuleResult] = []
    # normalised distance from each limit; averaged into `headroom` at the end
    slack: list[float] = []

    # record one rule result; only failures carry a fix type and a hint
    def add(rule_id: str, desc: str, ok: bool, *, part: Optional[str] = None, measured: Optional[float] = None,
            limit: str = "", fix: str = "auto", hint: str = "", severity: str = "hard") -> None:
        status = "pass" if ok else ("fail" if severity == "hard" else "warn")
        results.append(RuleResult(rule_id=rule_id, description=desc, status=status, severity=severity, part_id=part,
                                  measured=None if measured is None else round(measured, 3), limit=limit,
                                  fix=fix if not ok else "none", hint="" if ok else hint))

    # --- per-part rules --------------------------------------------------
    for p in spec.parts:
        t = p.nominal_wall_mm
        # check the thinnest and thickest spot, not just the nominal wall
        wall_ok = C.WALL_MIN_MM <= p.min_wall_mm and p.max_wall_mm <= C.WALL_MAX_MM
        add("WALL_RANGE", "Wall thickness within 1.5-3.0 mm", wall_ok, part=p.part_id, measured=t,
            limit=f"[{C.WALL_MIN_MM}, {C.WALL_MAX_MM}] mm (min {p.min_wall_mm}, max {p.max_wall_mm})",
            hint="clamp walls into 1.5-3.0 mm")
        slack.append(_clamp(min((p.min_wall_mm - C.WALL_MIN_MM) / C.WALL_MIN_MM, (C.WALL_MAX_MM - p.max_wall_mm) / C.WALL_MAX_MM)))

        for d in p.drafts:
            # Texture is declared per face: a textured finish applies to cosmetic outer faces, not to ribs or
            # core-side walls. Declaring an outer face of a textured part as smooth is caught by TEXTURE_DECLARED.
            textured = d.textured
            req = C.DRAFT_MIN_TEXTURED_DEG if textured else C.DRAFT_MIN_SMOOTH_DEG
            add("DRAFT_MIN", f"Draft on '{d.face}' >= {req} deg ({'textured' if textured else 'smooth'})",
                d.draft_deg >= req, part=p.part_id, measured=d.draft_deg, limit=f">= {req} deg",
                hint=f"raise draft on {d.face} to {req} deg")
            slack.append(_clamp((d.draft_deg - req) / req))

        # one well-drafted face says nothing about the others: both sides of the wall must be declared
        faces = [d.face for d in p.drafts]
        add("DRAFT_COVERAGE", "Draft declared for the outer and inner side walls",
            any(OUTER_FACE.search(f) for f in faces) and any(INNER_FACE.search(f) for f in faces),
            part=p.part_id, limit="outer + inner faces", hint="declare draft for outer_sides and inner_sides")

        if p.finish.textured:
            add("TEXTURE_DECLARED", "Textured finish declared on at least one moulded face",
                any(d.textured for d in p.drafts), part=p.part_id, limit="textured face present",
                hint="flag the cosmetic faces as textured so draft is checked against 2 deg")

        for r in p.ribs:
            # rib thickness relative to the wall it sits on
            ratio = r.thickness_mm / t
            add("RIB_RATIO", f"Rib '{r.rib_id}' thickness 40-60% of wall", C.RIB_RATIO_MIN <= ratio <= C.RIB_RATIO_MAX,
                part=p.part_id, measured=ratio, limit="[0.40, 0.60] x wall", hint="set rib thickness to 0.5 x wall")
            slack.append(_clamp(min(ratio - C.RIB_RATIO_MIN, C.RIB_RATIO_MAX - ratio) / 0.1))

        # sharp inside corners concentrate stress; radius scales with wall
        req_r = C.INNER_RADIUS_RATIO_MIN * t
        add("INNER_RADIUS", "Inner radius >= 0.5 x wall", p.inner_radius_mm >= req_r - 1e-9, part=p.part_id,
            measured=p.inner_radius_mm, limit=f">= {req_r:.2f} mm", hint="raise inner radius to 0.5 x wall")
        slack.append(_clamp((p.inner_radius_mm - req_r) / req_r))

        for u in p.undercuts:
            # an undercut is fine as long as the side action and its cost are written down
            declared = u.mechanism is not None and u.side_action_cost_cny is not None
            add("UNDERCUT_DECLARED", f"Undercut '{u.undercut_id}' declares side action and cost", declared,
                part=p.part_id, limit="mechanism + tooling cost declared",
                hint="declare slider/lifter and its tooling cost, or remove the undercut")

        if p.material not in contract.allowed_materials:
            add("MATERIAL_ALLOWED", f"Material {p.material.value} allowed by brief", False, part=p.part_id,
                limit=",".join(m.value for m in contract.allowed_materials), fix="design",
                hint="switch to an allowed material")

        if full:  # advisory checks, only on the full engine
            ratio = p.max_wall_mm / p.min_wall_mm
            add("WALL_UNIFORMITY", "Wall variation <= 25% (sink/warp risk)", ratio <= C.WALL_UNIFORMITY_MAX_RATIO,
                part=p.part_id, measured=ratio, limit="<= 1.25", severity="advisory", fix="none",
                hint="core out thick sections")
            for b in p.bosses:
                add("BOSS_RATIO", f"Boss '{b.boss_id}' OD/ID <= 2.5", b.outer_d_mm / b.inner_d_mm <= C.BOSS_OD_TO_ID_MAX,
                    part=p.part_id, measured=b.outer_d_mm / b.inner_d_mm, limit="<= 2.5", severity="advisory",
                    fix="none", hint="thin the boss wall or add gussets")

    # --- openings ---------------------------------------------------------
    dw = spec.display_window_mm
    add("DISPLAY_WINDOW_MIN", "Display window >= 42 x 18 mm",
        dw.width >= C.DISPLAY_MIN_W and dw.height >= C.DISPLAY_MIN_H, measured=dw.width * dw.height,
        limit=">= 42 x 18 mm", hint="enlarge display window to 42 x 18 mm")

    v = spec.vent
    add("VENT_ZONE_MIN", "Intake zone >= 18 x 18 mm", v.zone_w_mm >= C.INTAKE_MIN_W and v.zone_h_mm >= C.INTAKE_MIN_H,
        limit=">= 18 x 18 mm", hint="enlarge intake zone")
    # open area is recomputed here; whatever the model claimed is ignored
    oa = G.open_area_ratio(spec)
    add("VENT_OPEN_AREA", "Vent open area >= 15%", oa >= C.VENT_OPEN_AREA_MIN, measured=oa, limit=">= 0.15",
        hint="add slots or widen slots")
    slack.append(_clamp((oa - C.VENT_OPEN_AREA_MIN) / C.VENT_OPEN_AREA_MIN))
    a, b = G.face_dims(spec, v.face)
    add("VENT_FITS_FACE", f"Intake zone fits on the {getattr(v.face, 'value', v.face)} face with 2 mm margins",
        G.vent_fits_face(spec), limit=f"<= {a - 4:g} x {b - 4:g} mm", hint="shrink the intake zone to the face")
    add("VENT_SLOTS_FIT", "Vent slots fit the zone with >= 1 mm steel bars", G.vent_slots_fit(spec),
        limit="slots + bars <= zone", hint="reduce slot count or width")
    if full:
        add("SLOT_WIDTH_MIN", "Slot width >= 0.8 mm (tool steel strength)", v.slot_w_mm >= C.SLOT_WIDTH_MIN_MM,
            measured=v.slot_w_mm, limit=">= 0.8 mm", severity="advisory", fix="none", hint="widen slots")

    uo = spec.usb_opening_mm
    add("USB_OPENING_MIN", "USB-C opening >= 10 x 4 mm",
        uo.width >= C.USB_OPENING_MIN_W and uo.height >= C.USB_OPENING_MIN_H, measured=uo.width * uo.height,
        limit=">= 10 x 4 mm", hint="enlarge USB opening")
    # same for USB clearance
    clr = G.usb_clearance(spec)
    add("USB_CLEARANCE", "USB clearance >= 0.5 mm per side", clr >= C.USB_CLEARANCE_MIN_MM - 1e-9, measured=clr,
        limit=">= 0.5 mm", hint="enlarge USB opening to receptacle + 1.0 mm")
    slack.append(_clamp((clr - C.USB_CLEARANCE_MIN_MM) / C.USB_CLEARANCE_MIN_MM))

    # snap-fit hooks are undercuts: they need a lifter or bump-off, and that tooling has to be costed
    if spec.closure in (Closure.SNAP_FITS, Closure.HYBRID):
        add("SNAP_FIT_UNDERCUT", "Snap-fit closure declares its hooks as undercuts",
            any(p.undercuts for p in spec.parts), limit=">= 1 undercut", fix="design",
            hint="declare the snap-fit hooks as undercuts with mechanism and cost, or switch to screws")

    # --- sealing vs. ingress target ---------------------------------------
    # a sealed box still needs 15% open vents, so it needs a breathable membrane
    if contract.ip_rating in ("IP65", "IP67"):
        add("SEALING_FOR_IP", f"{contract.ip_rating} needs gasket + membrane vent (vent must stay >= 15% open)",
            spec.sealing == Sealing.GASKET_MEMBRANE, limit=Sealing.GASKET_MEMBRANE.value,
            hint="add gasket and ePTFE membrane vent")
    elif contract.ip_rating == "IP54":
        add("SEALING_FOR_IP", "IP54 needs at least a gasket", spec.sealing != Sealing.NONE, limit="gasket",
            hint="add gasket")

    # --- whole-product rules (need design trade-offs) ---------------------
    env, mx = spec.envelope_mm, contract.max_envelope_mm
    # the max box can be tighter than 125x75x30 if the brief asked for it
    env_ok = env.length <= mx.length and env.width <= mx.width and env.height <= mx.height
    add("ENVELOPE_MAX", f"Envelope <= {mx.length:g} x {mx.width:g} x {mx.height:g} mm", env_ok,
        measured=env.length * env.width * env.height,
        limit=f"<= {mx.length:g} x {mx.width:g} x {mx.height:g}", fix="design", hint="shrink the envelope")
    slack.append(_clamp(min((mx.length - env.length) / mx.length, (mx.width - env.width) / mx.width,
                            (mx.height - env.height) / mx.height) * 5))

    # can the PCB + battery actually go inside?
    pk = G.packaging(spec)
    add("PACKAGING_FIT", "PCB + battery stack fits the inner cavity", pk.fits, measured=pk.min_slack_mm,
        limit=f"cavity >= {pk.required[0]:.1f} x {pk.required[1]:.1f} x {pk.required[2]:.1f} mm", fix="design",
        hint="grow envelope height or reduce wall")
    slack.append(_clamp(pk.min_slack_mm / 3.0))

    hm = G.housing_mass_g(spec, densities)
    pm = G.product_mass_g(spec, densities)
    add("MASS_MAX", f"Product mass <= {contract.max_mass_g:g} g", pm <= contract.max_mass_g, measured=pm,
        limit=f"<= {contract.max_mass_g:g} g", fix="design", hint="reduce wall, drop bumper part, or shrink envelope")
    slack.append(_clamp((contract.max_mass_g - pm) / contract.max_mass_g * 5))

    # --- design-intent handoff integrity -------------------------------------
    anchors = list(must_preserve_anchor_ids)
    if anchors:
        valid_ids = spec.feature_ids()
        # an anchor only counts if it points at a feature that really exists in the spec
        traced = {t.anchor_id for t in spec.intent_trace if any(f in valid_ids for f in t.realized_by)}
        missing = [a for a in anchors if a not in traced]
        add("INTENT_TRACE_COMPLETE", "Every must-preserve intent anchor maps to a structural feature", not missing,
            measured=len(anchors) - len(missing), limit=f"{len(anchors)} anchors", fix="design",
            hint=f"map anchors {missing} to ids from this list only: {sorted(valid_ids)}")

    # one number for 'how far from the limits', tracked to spot specs creeping toward the edge
    headroom = round(sum(slack) / len(slack), 4) if slack else 0.0
    return DFMReport(
        variant_id=spec.variant_id, revision=spec.revision, results=results,
        engine="remote_full" if full else "local_baseline", confidence="full" if full else "reduced",
        headroom=headroom, housing_mass_g=hm, product_mass_g=pm, open_area_ratio=oa, usb_clearance_mm=clr,
    )
