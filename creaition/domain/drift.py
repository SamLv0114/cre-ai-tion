"""What the final structure dropped or changed from the 2D concept's CMF, checked by code.

The intent-trace rule only checks that each anchor points at a feature that exists. A reviser can still drop
a feature the concept was built around (e.g. the TPE overmould that makes the bumper) and describe the result
as "intent preserved" in its change log; the live run showed the judge believing that prose. This check
compares the two artifacts directly. Its findings go to the judge as facts and to the gate as a caveat, so a
variant whose look changed is shown to a person instead of being auto-approved.
"""

from __future__ import annotations

from .schemas import ConceptSpec, StructuralSpec


def concept_drift(concept: ConceptSpec, spec: StructuralSpec) -> list[str]:
    """Differences between the concept's material, finish and secondary operations and the final parts."""
    found: list[str] = []
    materials = {p.material for p in spec.parts}
    if concept.cmf.material not in materials:
        found.append(f"material {concept.cmf.material.value} -> {', '.join(sorted(m.value for m in materials))}")
    finishes = {p.finish for p in spec.parts}
    if concept.cmf.finish not in finishes:
        found.append(f"finish {concept.cmf.finish.value} -> {', '.join(sorted(f.value for f in finishes))}")
    ops = {op for p in spec.parts for op in p.secondary_ops}
    found += [f"dropped {op.value}" for op in concept.cmf.secondary_ops if op not in ops]
    return found
