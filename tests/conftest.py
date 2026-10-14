import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# make the package importable without installing it
sys.path.insert(0, str(ROOT))

from creaition.domain.schemas import (  # noqa: E402
    CMF, ConceptSpec, DesignBrief, DesignContract, DesignIntent, Dims2, Dims3, Face, Finish, IntentAnchor, Material,
    Placement, Priorities, Silhouette,
)


# the six real evaluation briefs, keyed by id
@pytest.fixture
def briefs() -> dict[str, DesignBrief]:
    out = {}
    for f in sorted((ROOT / "eval" / "briefs").glob("*.json")):
        b = DesignBrief.model_validate(json.loads(f.read_text()))
        out[b.brief_id] = b
    return out


# a plain, balanced brief used by the unit tests
@pytest.fixture
def intent() -> DesignIntent:
    return DesignIntent(
        brief_id="T1", summary="test brief for a desk detector",
        priorities=Priorities(cost=0.5, durability=0.5, aesthetics=0.5, sustainability=0.3),
        environment="indoor", allowed_materials=[Material.ABS, Material.PC_ABS, Material.RABS],
        preferred_finishes=[Finish.MATTE], style_keywords=["clean"],
    )


@pytest.fixture
def contract(intent) -> DesignContract:
    return DesignContract(brief_id="T1", intent=intent, cost_ceiling_cny=18.0,
                          max_envelope_mm=Dims3(length=125, width=75, height=30), max_mass_g=180.0,
                          allowed_materials=intent.allowed_materials, ip_rating="none", floor_cost_cny=9.4)


# a simple pebble concept with two intent anchors
@pytest.fixture
def concept() -> ConceptSpec:
    return ConceptSpec(
        variant_id="T1-v001", brief_id="T1", direction="soft_pebble", name="Test Pebble", form_language="soft pebble",
        silhouette=Silhouette.PEBBLE, envelope_mm=Dims3(length=116, width=66, height=27), corner_radius_mm=12,
        display=Placement(face=Face.FRONT, x_pct=50, y_pct=35), display_window_mm=Dims2(width=44, height=20),
        intake=Placement(face=Face.FRONT, x_pct=50, y_pct=78), usb=Placement(face=Face.BOTTOM, x_pct=50, y_pct=50),
        cmf=CMF(material=Material.ABS, finish=Finish.MATTE, color="white"),
        intent_anchors=[IntentAnchor(anchor_id="IA1", statement="pebble form", category="form"),
                        IntentAnchor(anchor_id="IA2", statement="readable display", category="usability")],
        rationale="test concept rationale",
    )
