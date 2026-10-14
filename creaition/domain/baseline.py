"""Deterministic, known-good fallbacks.

`baseline_structure` is the safe template the pipeline falls back to when the
LLM structural modeler is unavailable, or when the critic-and-revise loop hits
its cap. It always satisfies the closed-form DFM rules by construction; whether
it fits the brief's cost / mass / intent is still checked like any other spec.
Fallback output is flagged `degraded` and can never be auto-approved.
"""

from __future__ import annotations

from . import catalog as K
from . import constants as C
from . import cost_model
from . import geometry as G
from .schemas import (
    Boss, Closure, ConceptSpec, DesignContract, Dims2, Dims3, FaceDraft, Finish, IntentTrace, Material, PartSpec, Rib,
    Sealing, SecondaryOp, StructuralSpec, VentSpec,
)

# the only decorations the fallback keeps; everything else costs real money
CHEAP_OPS = {SecondaryOp.PAD_PRINT, SecondaryOp.LASER_ETCH}
# default feature to point each kind of intent anchor at
_ANCHOR_FEATURE = {
    "form": "envelope",
    "usability": "display_window",
    "cmf": "front_housing.finish",
    "durability": "rear_housing",
    "sustainability": "front_housing.material",
    "cost": "closure",
}


# pick the sealing the IP target requires
def sealing_for(ip_rating: str) -> Sealing:
    if ip_rating in ("IP65", "IP67"):
        return Sealing.GASKET_MEMBRANE
    if ip_rating == "IP54":
        return Sealing.GASKET
    return Sealing.NONE


def cheapest_material(allowed: list[Material]) -> Material:
    return min(allowed, key=lambda m: K.MATERIALS_SNAPSHOT[m].price_cny_per_kg)


# comfortably above the 1 / 2 degree minimums
def safe_drafts(finish: Finish) -> list[FaceDraft]:
    deg = 2.5 if finish.textured else 1.5
    return [FaceDraft(face=f, draft_deg=deg, textured=finish.textured) for f in ("outer_sides", "inner_sides", "ribs")]


# a shell part that passes every closed-form rule by construction
def _part(part_id: str, role: str, material: Material, finish: Finish, wall: float, ops: list[SecondaryOp]) -> PartSpec:
    return PartSpec(
        part_id=part_id, role=role, material=material, finish=finish, nominal_wall_mm=wall, min_wall_mm=wall,
        max_wall_mm=wall, inner_radius_mm=round(0.6 * wall, 2), drafts=safe_drafts(finish),
        ribs=[Rib(rib_id=f"{part_id}_rib", thickness_mm=round(0.5 * wall, 2), height_mm=6.0, length_mm=40.0, count=4)],
        bosses=[Boss(boss_id=f"{part_id}_boss", outer_d_mm=5.0, inner_d_mm=2.5, height_mm=8.0, count=4)],
        undercuts=[], secondary_ops=ops,
    )


def baseline_structure(concept: ConceptSpec, contract: DesignContract, revision: int, note: str) -> StructuralSpec:
    # middle of the 1.5-3.0 range, so small changes later don't break it
    wall = 2.0
    material = concept.cmf.material if concept.cmf.material in contract.allowed_materials else cheapest_material(
        contract.allowed_materials)
    finish = concept.cmf.finish
    # keep the concept's look where we can, drop costly decoration
    ops = [op for op in concept.cmf.secondary_ops if op in CHEAP_OPS]
    # grow the concept's box if it's too small for the parts, but never past the max
    min_env = G.min_envelope_for_wall(wall)
    mx = contract.max_envelope_mm
    env = Dims3(
        length=min(max(concept.envelope_mm.length, min_env.length), mx.length),
        width=min(max(concept.envelope_mm.width, min_env.width), mx.width),
        height=min(max(min_env.height + 0.5, min(concept.envelope_mm.height, min_env.height + 2)), mx.height),
    )
    parts = [
        _part("front_housing", "front_housing", material, finish, wall, ops),
        _part("rear_housing", "rear_housing", material, finish, wall, []),
    ]
    # map every must-keep anchor so the intent-trace rule passes
    trace = [
        IntentTrace(anchor_id=a.anchor_id, realized_by=[_ANCHOR_FEATURE[a.category]], note="baseline mapping")
        for a in concept.intent_anchors
        if a.must_preserve
    ]
    return StructuralSpec(
        variant_id=concept.variant_id, brief_id=concept.brief_id, concept_name=concept.name, revision=revision,
        envelope_mm=env, parting="front_back", parts=parts,
        display_window_mm=Dims2(width=max(C.DISPLAY_MIN_W, concept.display_window_mm.width),
                                height=max(C.DISPLAY_MIN_H, concept.display_window_mm.height)),
        vent=VentSpec(face=concept.intake.face, zone_w_mm=20.0, zone_h_mm=20.0, slot_count=7, slot_w_mm=1.2,
                      slot_l_mm=16.0),
        usb_opening_mm=Dims2(width=11.0, height=5.0), closure=Closure.SCREWS, screw_count=4,
        sealing=sealing_for(contract.ip_rating), intent_trace=trace,
        change_log=[f"deterministic baseline: {note}"],
    )


def floor_spec(allowed: list[Material], ip_rating: str, *, by: str = "cost") -> StructuralSpec:
    """Smallest, plainest housing that can physically exist for this material set (a lower bound)."""
    wall = C.WALL_MIN_MM
    if by == "cost":
        material = cheapest_material(allowed)
    else:
        material = min(allowed, key=lambda m: K.MATERIALS_SNAPSHOT[m].density_g_cm3)
    env = G.min_envelope_for_wall(wall)
    parts = []
    for role in ("front_housing", "rear_housing"):
        parts.append(PartSpec(
            part_id=role, role=role, material=material, finish=Finish.MATTE, nominal_wall_mm=wall, min_wall_mm=wall,
            max_wall_mm=wall, inner_radius_mm=1.0, drafts=safe_drafts(Finish.MATTE)))
    return StructuralSpec(
        variant_id="floor", brief_id="floor", concept_name="floor", revision=0, envelope_mm=env, parting="front_back",
        parts=parts, display_window_mm=Dims2(width=42, height=18),
        vent=VentSpec(face="front", zone_w_mm=18, zone_h_mm=18, slot_count=6, slot_w_mm=1.2, slot_l_mm=14),
        usb_opening_mm=Dims2(width=10, height=4.3), closure=Closure.SCREWS, screw_count=2,
        sealing=sealing_for(ip_rating),
    )


# cheapest housing that can exist; a target below this is impossible
def floor_cost_cny(allowed: list[Material], ip_rating: str) -> float:
    spec = floor_spec(allowed, ip_rating, by="cost")
    quote = cost_model.tooling_quote_from_rates(spec)
    return cost_model.estimate(spec, K.MATERIALS_SNAPSHOT, quote, ceiling_cny=1e9).total_cny


# lightest product that can exist with these materials
def floor_mass_g(allowed: list[Material], ip_rating: str) -> float:
    spec = floor_spec(allowed, ip_rating, by="mass")
    return G.product_mass_g(spec, {m: r.density_g_cm3 for m, r in K.MATERIALS_SNAPSHOT.items()})


# thinnest product that still fits the stack (1.5 mm walls)
def min_feasible_height_mm() -> float:
    return G.min_envelope_for_wall(C.WALL_MIN_MM).height
