"""Deterministic geometry: volumes, mass, clearances, open area.

Plain arithmetic on the structural spec. Nothing here is delegated to a model:
an LLM that *claims* 18 % open area is not evidence of 18 % open area.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import constants as C
from .schemas import Dims3, Material, PartSpec, Sealing, SecondaryOp, StructuralSpec


@dataclass(frozen=True)
class PackagingCheck:
    fits: bool
    cavity: tuple[float, float, float]
    required: tuple[float, float, float]
    min_slack_mm: float


# the thicker of the two shell walls decides how much room is left inside
def housing_wall(spec: StructuralSpec) -> float:
    return max(p.nominal_wall_mm for p in spec.parts if p.role in ("front_housing", "rear_housing"))


# inside space needed: PCB stacked on the battery, plus clearance on every side
def required_cavity() -> tuple[float, float, float]:
    clr = 2 * C.PACKAGING_CLEARANCE_MM
    return (
        C.PCB.length + clr,
        C.PCB.width + clr,
        C.PCB.height + C.BATTERY.height + C.STACK_GAP_MM + clr,
    )


# smallest outer box that still fits the stack at this wall thickness
def min_envelope_for_wall(wall_mm: float) -> Dims3:
    req = required_cavity()
    return Dims3(length=req[0] + 2 * wall_mm, width=req[1] + 2 * wall_mm, height=req[2] + 2 * wall_mm)


def packaging(spec: StructuralSpec) -> PackagingCheck:
    t = housing_wall(spec)
    env = spec.envelope_mm
    cavity = (env.length - 2 * t, env.width - 2 * t, env.height - 2 * t)
    req = required_cavity()
    # tightest axis wins; negative slack means the parts don't fit
    slack = min(c - r for c, r in zip(cavity, req))
    return PackagingCheck(fits=slack >= 0, cavity=cavity, required=req, min_slack_mm=round(slack, 3))


def face_dims(spec: StructuralSpec, face: str) -> tuple[float, float]:
    """Outer dimensions (long, short) of the named face of the envelope."""
    e = spec.envelope_mm
    face = getattr(face, "value", face)
    if face in ("front", "back"):
        dims = (e.length, e.width)
    elif face in ("top", "bottom"):
        dims = (e.length, e.height)
    else:
        dims = (e.width, e.height)
    return max(dims), min(dims)


def vent_fits_face(spec: StructuralSpec, margin_mm: float = 2.0) -> bool:
    a, b = face_dims(spec, spec.vent.face)
    w, h = spec.vent.zone_w_mm, spec.vent.zone_h_mm
    # the zone may be placed either way round on the face
    return (w <= a - 2 * margin_mm and h <= b - 2 * margin_mm) or (h <= a - 2 * margin_mm and w <= b - 2 * margin_mm)


def open_area_ratio(spec: StructuralSpec) -> float:
    v = spec.vent
    zone = v.zone_w_mm * v.zone_h_mm
    if zone <= 0:
        return 0.0
    return round(v.slot_count * v.slot_w_mm * v.slot_l_mm / zone, 4)


def vent_slots_fit(spec: StructuralSpec) -> bool:
    v = spec.vent
    if v.slot_count == 0:
        return True
    # slots run along the zone width; leave a steel bar at each end
    fits_len = v.slot_l_mm <= v.zone_w_mm - 2 * C.VENT_MIN_BAR_MM + 1e-9
    # N slots stacked need N+1 bars between and around them
    fits_stack = v.slot_count * v.slot_w_mm + (v.slot_count + 1) * C.VENT_MIN_BAR_MM <= v.zone_h_mm + 1e-9
    return fits_len and fits_stack


def usb_clearance(spec: StructuralSpec) -> float:
    o = spec.usb_opening_mm
    # gap per side, on the tighter of the two axes
    return round(min((o.width - C.USB_RECEPTACLE_W) / 2, (o.height - C.USB_RECEPTACLE_H) / 2), 3)


# hollow box volume split evenly between the front and rear shell
def _shell_half_volume_mm3(env: Dims3, t: float) -> float:
    outer = env.length * env.width * env.height
    inner = max(env.length - 2 * t, 0) * max(env.width - 2 * t, 0) * max(env.height - 2 * t, 0)
    return 0.5 * (outer - inner)


def part_volume_cm3(part: PartSpec, spec: StructuralSpec) -> float:
    env = spec.envelope_mm
    t = part.nominal_wall_mm
    if part.role == "bumper":
        # Perimeter band covering ~half the height.
        vol = 2 * (env.length + env.width) * 0.5 * env.height * t
    else:
        vol = _shell_half_volume_mm3(env, t)
        if part.role == "front_housing":
            # the display window is cut out of the front shell
            vol -= spec.display_window_mm.width * spec.display_window_mm.height * t
    for r in part.ribs:
        vol += r.thickness_mm * r.height_mm * r.length_mm * r.count
    for b in part.bosses:
        vol += math.pi / 4 * (b.outer_d_mm**2 - b.inner_d_mm**2) * b.height_mm * b.count
    return max(vol, 0.0) / 1000.0


# rough TPE band: 6 mm tall, 1.5 mm thick, all the way round
def tpe_overmold_mass_g(spec: StructuralSpec) -> float:
    if not any(SecondaryOp.TPE_OVERMOLD in p.secondary_ops for p in spec.parts):
        return 0.0
    env = spec.envelope_mm
    band_mm3 = 2 * (env.length + env.width) * 6.0 * 1.5
    return band_mm3 / 1000.0 * C.BUMPER_TPE_DENSITY


def housing_mass_g(spec: StructuralSpec, densities: dict[Material, float]) -> float:
    return round(sum(part_volume_cm3(p, spec) * densities[p.material] for p in spec.parts), 2)


def product_mass_g(spec: StructuralSpec, densities: dict[Material, float]) -> float:
    mass = housing_mass_g(spec, densities) + C.INTERNAL_COMPONENT_MASS_G + tpe_overmold_mass_g(spec)
    if spec.sealing != Sealing.NONE:
        mass += C.GASKET_MASS_G
    mass += 2.0  # display cover lens (PMMA)
    return round(mass, 2)
