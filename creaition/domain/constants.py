"""Fixed product, packaging and DFM constraints from the assessment brief.

This module is the single source of truth for every hard number. Agents never
restate these numbers from memory; prompts, validators and the cost model all
import them from here so a rule change is a one-line diff.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    length: float
    width: float
    height: float


# --- Package (mm) -----------------------------------------------------------
PCB = Box(92.0, 45.0, 12.0)
BATTERY = Box(60.0, 35.0, 8.0)
DISPLAY_MIN_W, DISPLAY_MIN_H = 42.0, 18.0
INTAKE_MIN_W, INTAKE_MIN_H = 18.0, 18.0
USB_OPENING_MIN_W, USB_OPENING_MIN_H = 10.0, 4.0
# Assumption: USB-C receptacle shell nominal size (USB-IF spec 8.34 x 2.56 shell,
# we use the common mid-mount receptacle body incl. tolerance).
USB_RECEPTACLE_W, USB_RECEPTACLE_H = 8.94, 3.26
ENVELOPE_MAX = Box(125.0, 75.0, 30.0)
PRODUCT_MASS_MAX_G = 180.0

# --- Internal packaging assumptions (documented in DECISIONS.md) --------------
PACKAGING_CLEARANCE_MM = 0.5  # gap between a component and the inner wall, per side
STACK_GAP_MM = 1.0  # foam / insulation sheet between battery and PCB
# PCB 28 g + battery 42 g + display 9 g + sensor/fan module 10 g + fasteners/misc 3 g
INTERNAL_COMPONENT_MASS_G = 92.0
BUMPER_TPE_DENSITY = 1.10  # g/cm3
GASKET_MASS_G = 3.0

# --- Manufacturing -------------------------------------------------------------
ANNUAL_VOLUME = 10_000
TOOLING_AMORTIZATION_YEARS = 2  # tooling written off over 20,000 units
BOM_CEILING_CNY = 18.0
# When a brief limit has to be relaxed, give it this much room above the bare-minimum design (smallest form,
# no finishes). Relaxing to the floor exactly would leave no room for the brief's own finishes and features.
RELAX_HEADROOM = 0.25

# --- Simplified DFM baseline -------------------------------------------------
WALL_MIN_MM, WALL_MAX_MM = 1.5, 3.0
DRAFT_MIN_SMOOTH_DEG, DRAFT_MIN_TEXTURED_DEG = 1.0, 2.0
RIB_RATIO_MIN, RIB_RATIO_MAX = 0.40, 0.60
INNER_RADIUS_RATIO_MIN = 0.5
VENT_OPEN_AREA_MIN = 0.15
USB_CLEARANCE_MIN_MM = 0.5
VENT_MIN_BAR_MM = 1.0  # minimum steel bar between vent slots (tool strength)

# --- Advisory (warn-only) checks run by the full "remote" DFM engine ----------
WALL_UNIFORMITY_MAX_RATIO = 1.25
SLOT_WIDTH_MIN_MM = 0.8
BOSS_OD_TO_ID_MAX = 2.5
