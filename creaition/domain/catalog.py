"""Reference data: materials, tooling and process rates.

In production these come from a PLM / supplier-quote service (see tools/). The
copy in this module doubles as the *bundled snapshot* the tools degrade to when
the live service times out. All prices are assumptions for the exercise
(CNY, 2026), chosen so the CNY 18 housing-BOM ceiling is a real constraint.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .schemas import Material, SecondaryOp


class MaterialRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    material: Material
    density_g_cm3: float
    price_cny_per_kg: float
    recycled_content_pct: int
    impact_rating: int  # 1..5 (notched Izod, relative)
    uv_rating: int  # 1..5
    co2e_kg_per_kg: float
    price_as_of: str


MATERIALS_SNAPSHOT: dict[Material, MaterialRecord] = {
    Material.ABS: MaterialRecord(
        material=Material.ABS, density_g_cm3=1.05, price_cny_per_kg=15.5, recycled_content_pct=0,
        impact_rating=3, uv_rating=2, co2e_kg_per_kg=3.3, price_as_of="2026-09-30",
    ),
    Material.PC_ABS: MaterialRecord(
        material=Material.PC_ABS, density_g_cm3=1.15, price_cny_per_kg=24.0, recycled_content_pct=0,
        impact_rating=5, uv_rating=3, co2e_kg_per_kg=4.6, price_as_of="2026-09-30",
    ),
    Material.RABS: MaterialRecord(
        material=Material.RABS, density_g_cm3=1.05, price_cny_per_kg=17.0, recycled_content_pct=70,
        impact_rating=2, uv_rating=2, co2e_kg_per_kg=1.4, price_as_of="2026-09-30",
    ),
}

# Tooling (one-off, CNY). Single-cavity P20 steel moulds, 10k/yr is low volume.
MOULD_BASE_CNY = {"front_housing": 55_000.0, "rear_housing": 48_000.0, "bumper": 30_000.0}
SIDE_ACTION_CNY = {"slider": 12_000.0, "lifter": 9_000.0, "bump_off": 1_500.0}
TEXTURE_PER_PART_CNY = 4_000.0
TPE_2K_TOOLING_CNY = 40_000.0

# Per-unit process and hardware rates (CNY).
MACHINE_RATE_CNY_PER_HOUR = 95.0
RUNNER_SCRAP_FACTOR = 1.08
QA_OVERHEAD = 0.03
SECONDARY_OP_CNY = {
    SecondaryOp.PAINT: 2.8,
    SecondaryOp.SOFT_TOUCH: 3.6,
    SecondaryOp.PAD_PRINT: 0.4,
    SecondaryOp.LASER_ETCH: 0.3,
    SecondaryOp.IML: 1.6,
    SecondaryOp.TPE_OVERMOLD: 1.9,
}
SCREW_CNY = 0.12
WINDOW_LENS_CNY = 1.0
GASKET_CNY = 1.2
MEMBRANE_VENT_CNY = 1.5
ASSEMBLY_BASE_CNY = 0.6
ASSEMBLY_PER_SCREW_CNY = 0.08
ASSEMBLY_GASKET_CNY = 0.4
ASSEMBLY_SNAP_CNY = 0.2


def cycle_time_s(wall_mm: float) -> float:
    """Cooling time dominates and scales with wall thickness squared."""
    return 18.0 + 5.0 * wall_mm * wall_mm
