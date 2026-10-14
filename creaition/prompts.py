"""Versioned prompts, one per agent role. v2 = v1 plus a DFM checklist (structure) and a reference design (concept).

Hard numbers come from constants.py, so a prompt can't disagree with the rule engine. The prompt hash is
part of every model step's idempotency key, so editing a prompt invalidates exactly the checkpoints it should.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel

from .domain import constants as C
from .reliability.idempotency import digest

# built from constants.py so the prompt and the rule engine can never disagree
DFM_CHECKLIST = f"""DFM checklist (every item is verified by a deterministic rule engine after you answer):
- wall thickness {C.WALL_MIN_MM}-{C.WALL_MAX_MM} mm on every part (min_wall_mm and max_wall_mm included)
- draft >= {C.DRAFT_MIN_SMOOTH_DEG} deg on smooth faces, >= {C.DRAFT_MIN_TEXTURED_DEG} deg on textured faces; every part declares draft for both outer_sides and inner_sides
- a snap-fit closure declares each hook as an undercut (mechanism + side_action_cost_cny)
- rib thickness {int(C.RIB_RATIO_MIN * 100)}-{int(C.RIB_RATIO_MAX * 100)}% of the nominal wall
- inner radius >= {C.INNER_RADIUS_RATIO_MIN} x wall
- intake zone >= {C.INTAKE_MIN_W:g} x {C.INTAKE_MIN_H:g} mm with open area >= {int(C.VENT_OPEN_AREA_MIN * 100)}% \
(slot_count x slot_w x slot_l / zone area), >= {C.VENT_MIN_BAR_MM:g} mm steel between slots
- USB-C opening >= {C.USB_OPENING_MIN_W:g} x {C.USB_OPENING_MIN_H:g} mm and >= {C.USB_CLEARANCE_MIN_MM} mm clearance \
per side around a {C.USB_RECEPTACLE_W} x {C.USB_RECEPTACLE_H} mm receptacle
- display window >= {C.DISPLAY_MIN_W:g} x {C.DISPLAY_MIN_H:g} mm
- every undercut declares mechanism (slider | lifter | bump_off) and side_action_cost_cny
- the PCB {C.PCB.length:g}x{C.PCB.width:g}x{C.PCB.height:g} mm and battery {C.BATTERY.length:g}x{C.BATTERY.width:g}x\
{C.BATTERY.height:g} mm stack inside: envelope height >= {C.PCB.height + C.BATTERY.height + C.STACK_GAP_MM + 2 * C.PACKAGING_CLEARANCE_MM:g} mm + 2 x wall"""

PRODUCT_FACTS = f"""Product: portable air-quality detector housing, injection moulded (ABS, PC-ABS or recycled ABS "rABS"),
{C.ANNUAL_VOLUME:,} units/year, housing BOM ceiling CNY {C.BOM_CEILING_CNY:g}/unit.
Envelope <= {C.ENVELOPE_MAX.length:g} x {C.ENVELOPE_MAX.width:g} x {C.ENVELOPE_MAX.height:g} mm, product mass <= \
{C.PRODUCT_MASS_MAX_G:g} g (internals weigh ~{C.INTERNAL_COMPONENT_MASS_G:g} g)."""

JSON_RULES = """Respond with ONE JSON object only, no prose and no code fences. It must validate against this JSON Schema:
{schema}"""

REFERENCE_CONCEPT = {
    "name": "Reference Desk Unit", "silhouette": "rounded_rect",
    "envelope_mm": {"length": 116, "width": 66, "height": 27}, "corner_radius_mm": 8,
    "display_window_mm": {"width": 44, "height": 20},
    "cmf": {"material": "ABS", "finish": "smooth_matte", "color": "warm white", "secondary_ops": ["pad_print"]},
}


# system + user text for one role and version; the hash goes into idempotency keys
@dataclass(frozen=True)
class PromptTemplate:
    role: str
    version: str
    system: str
    user: str

    @property
    def hash(self) -> str:
        return digest(self.role, self.version, self.system, self.user, length=12)


# role instructions (static, so the provider can cache them)
_SYSTEM = {
    "brief_interpreter": (
        "You are the Brief Interpreter for an industrial-design pipeline. Turn a designer's free-text brief into a "
        "structured design intent. Extract numeric constraints exactly as written (do not invent numbers). Flag "
        "requirements that cannot all be satisfied at once as conflicts (severity 'hard' if physically or "
        "economically infeasible), and list open questions with the default assumption you would use.\n"
        + PRODUCT_FACTS
    ),
    "concept_generator": (
        "You are the 2D Concept Generator. Produce ONE distinct concept for the given style direction: silhouette, "
        "envelope, front-face layout (display, intake, USB) and CMF. Name 2-6 intent anchors (IA1, IA2, ...): the "
        "specific design decisions a downstream engineer must not lose. Stay inside the design contract.\n"
        + PRODUCT_FACTS
    ),
    "structural_modeler": (
        "You are the 3D-Readiness Modeler. Convert a 2D concept into a structural specification a CAD engineer can "
        "build: parts, walls, draft per face, ribs, bosses, undercuts, vent geometry, openings, closure and sealing. "
        "Map EVERY intent anchor of the concept to the feature ids that realise it in intent_trace (feature ids: part "
        "ids, rib/boss/undercut ids, 'envelope', 'display_window', 'vent', 'usb_opening', 'closure', 'sealing', "
        "'parting', '<part_id>.finish', '<part_id>.material', '<part_id>.<secondary_op>').\n" + PRODUCT_FACTS
    ),
    "reviser": (
        "You are the Design Reviser in a bounded critic-and-revise loop. A deterministic DFM/cost critic found the "
        "failures listed. Fix them with the smallest change that preserves the concept's intent anchors; when you "
        "must trade intent away (e.g. drop a coating for budget), say so in `changes` and keep intent_trace valid. "
        "You may call a tool first (materials_lookup {material} or cost_what_if {material?, drop_secondary_ops?, "
        "remove_bumper?}) to price an option, at most {max_tools} tool calls in total. Respond with either "
        '{"step": {"action": "tool_call", ...}} or {"step": {"action": "final", "structure": <full revised '
        'StructuralSpec>, "changes": [...]}}. Increment `revision`.\n' + PRODUCT_FACTS
    ),
    "quality_judge": (
        "You are the Quality Judge. Score the variant against the brief with this rubric (integers 1-5):\n"
        "- brief_adherence: does the spec deliver what the brief prioritises (counts double)\n"
        "- form_coherence: silhouette, proportions and radii read as one idea\n"
        "- cmf_fit: material/finish/colour suit the use context and priorities\n"
        "- usability: display readable, intake unobstructed in normal use, USB reachable\n"
        "- intent_preservation: the 3D spec still carries the concept's intent anchors after any revision\n"
        "Engineering compliance and cost are already verified by code; do NOT score them. verdict: accept "
        "(weighted mean >= 3.5), weak (>= 2.8) or reject. Judge the spec, not its prose.\n"
        "The detector draws room air through its intake past the sensor/fan module: an intake that a hand, desk or "
        "strap covers in normal use gives wrong readings.\n"
        "code_checked_changes lists what the final structure dropped or changed from the concept's CMF, found by "
        "comparing the two specs. Trust it over the change_log wording when you score intent_preservation.\n"
        + PRODUCT_FACTS
    ),
    "explainer": (
        "You answer questions about a pipeline run using ONLY the facts provided. Cite variant ids. If the facts do "
        "not answer the question, say so."
    ),
}

# Prompt layout is ordered for provider prefix caching: everything static per role/version (instructions,
# product facts, output schema) lives in the system message; the user message puts brief-level content
# shared by the whole fan-out (the contract) before variant-specific content.
# per-call content; shared contract first, variant-specific bits last
_USER = {
    "brief_interpreter": "Brief:\n{input}",
    "concept_generator": "{lessons}\nDesign contract, then this variant's direction:\n{input}",
    "structural_modeler": "{lessons}\nDesign contract, then the concept to convert:\n{input}",
    "reviser": DFM_CHECKLIST + "\n{lessons}\nCurrent state:\n{input}",
    "quality_judge": "Variant to judge:\n{input}",
    "explainer": "Question and facts:\n{input}",
}

# the only difference between v1 and v2
_V2_EXTRA = {
    "concept_generator": "\nA DFM-safe reference design that tooling has already approved (adapt, do not copy "
                         "blindly):\n" + json.dumps(REFERENCE_CONCEPT),
    "structural_modeler": "\n" + DFM_CHECKLIST,
}


def get_template(role: str, version: str) -> PromptTemplate:
    system = _SYSTEM[role] + "\n\n" + JSON_RULES
    user = _USER[role]
    if version == "v2" and role in _V2_EXTRA:
        system = system + "\n" + _V2_EXTRA[role]
    return PromptTemplate(role=role, version=version, system=system, user=user)


def prompt_hash(role: str, version: str) -> str:
    return get_template(role, version).hash


def render(role: str, version: str, inputs: dict[str, Any], schema: type[BaseModel],
           lessons: Iterable[Any] = (), **fmt: Any) -> tuple[list[dict[str, str]], str]:
    t = get_template(role, version)
    lesson_lines = [f"- {l.text}" for l in lessons]
    # retrieved lessons are pasted in as plain bullet points
    lesson_block = ("Lessons from previous runs and designers (follow them):\n" + "\n".join(lesson_lines)
                    if lesson_lines else "")
    # the exact JSON Schema the reply will be validated against
    schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    system = t.system.replace("{schema}", schema_json)
    for k, v in fmt.items():  # explicit placeholders only; prompts contain literal JSON braces
        system = system.replace("{" + k + "}", str(v))
    user = t.user.replace("{input}", json.dumps(inputs, indent=1, default=str)).replace("{lessons}", lesson_block)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], t.hash


# sent back with the validation errors when a reply doesn't parse
REPAIR_PROMPT = ("Your previous reply could not be used: {errors}\n"
                 "Return the corrected, complete JSON object only. Keep every value you did not need to change.")
