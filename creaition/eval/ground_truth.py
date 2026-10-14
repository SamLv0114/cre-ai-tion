"""Deterministic ground truth for brief adherence, and planted-defect probes.

These rules are written independently of the judge's rubric text. They are a
narrow, checkable proxy for "does the spec deliver the brief's top priority":
if the LLM judge and these rules disagree often, the judge is not trustworthy
enough to gate on.
"""

from __future__ import annotations

from typing import Optional

from ..domain.schemas import (
    Closure, ConceptSpec, CostReport, DesignContract, Finish, Material, Sealing, SecondaryOp, StructuralSpec,
)

PREMIUM_OPS = {SecondaryOp.SOFT_TOUCH, SecondaryOp.PAINT, SecondaryOp.IML}
COATINGS = {SecondaryOp.PAINT, SecondaryOp.SOFT_TOUCH, SecondaryOp.IML}


def adherence_gt(archetype: str, contract: DesignContract, concept: ConceptSpec, spec: StructuralSpec,
                 cost: Optional[CostReport]) -> Optional[bool]:
    mats = {p.material for p in spec.parts}
    ops = {o for p in spec.parts for o in p.secondary_ops}
    finish = spec.parts[0].finish
    wall = max(p.nominal_wall_mm for p in spec.parts)
    if archetype == "cost_sensitive":
        # cheap: within target, cheap resin, no side actions, no coatings
        target = contract.intent.target_unit_cost_cny or contract.cost_ceiling_cny
        return (cost is not None and cost.total_cny <= target and mats <= {Material.ABS, Material.RABS}
                and not any(p.undercuts for p in spec.parts) and not (ops & COATINGS))
    if archetype == "rugged":
        # rugged: PC-ABS, textured, thick walls, sealed
        return (Material.PC_ABS in mats and finish.textured and wall >= 2.2 and spec.sealing != Sealing.NONE)
    if archetype == "premium_home":
        # premium: smooth, a premium finish, slim, no visible screws
        return (not finish.textured and bool(ops & PREMIUM_OPS) and spec.envelope_mm.height <= 27.5
                and spec.closure != Closure.SCREWS)
    if archetype == "sustainable":
        # sustainable: recycled ABS only, no coatings, easy to take apart
        return mats == {Material.RABS} and not (ops & COATINGS) and spec.closure in (Closure.SNAP_FITS, Closure.SCREWS)
    if archetype == "balanced":
        # balanced: 3 of 4 sensible choices is enough
        checks = [finish in (Finish.TEXTURE_LIGHT, Finish.MATTE), cost is not None and cost.total_cny <= 15,
                  1.8 <= wall <= 2.5, concept.intake.face.value != "bottom"]
        return sum(checks) >= 3
    return None  # conflicting brief: adherence is undefined by construction


def plant_defect(archetype: str, spec: StructuralSpec) -> Optional[StructuralSpec]:
    """Mutate a good spec so it clearly violates the brief's top priority (judge sensitivity probe)."""
    # apply the same change to every part
    def parts(**upd) -> list:
        return [p.model_copy(update={k: (v(p) if callable(v) else v) for k, v in upd.items()}) for p in spec.parts]

    # each mutation breaks exactly what that kind of brief cares about most
    if archetype == "cost_sensitive":
        new = parts(material=Material.PC_ABS, secondary_ops=[SecondaryOp.SOFT_TOUCH, SecondaryOp.PAINT])
    elif archetype == "rugged":
        new = parts(material=Material.ABS, finish=Finish.GLOSS, nominal_wall_mm=1.6, min_wall_mm=1.6, max_wall_mm=1.6)
        return spec.model_copy(update={"parts": new, "sealing": Sealing.NONE, "change_log": ["planted defect"]})
    elif archetype == "premium_home":
        new = parts(finish=Finish.TEXTURE_HEAVY, secondary_ops=[])
        return spec.model_copy(update={"parts": new, "closure": Closure.SCREWS, "screw_count": 6,
                                       "envelope_mm": spec.envelope_mm.model_copy(update={"height": 30.0}),
                                       "change_log": ["planted defect"]})
    elif archetype == "sustainable":
        new = [p.model_copy(update={"material": Material.PC_ABS if i == 0 else p.material,
                                    "secondary_ops": [SecondaryOp.PAINT]}) for i, p in enumerate(spec.parts)]
    elif archetype == "balanced":
        new = parts(finish=Finish.GLOSS, nominal_wall_mm=3.0, min_wall_mm=3.0, max_wall_mm=3.0,
                    secondary_ops=[SecondaryOp.IML, SecondaryOp.PAINT])
    else:
        return None
    return spec.model_copy(update={"parts": new, "change_log": ["planted defect"]})
