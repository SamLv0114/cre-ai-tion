"""Offline stand-in for the real models, so the graded run needs no key and gives the same result every time.

It builds answers from the structured payload with simple rules, and it makes mistakes on purpose (DFM
slips, design errors, imperfect revisions, a biased and noisy judge) so the repair loop and the judge checks
have something to catch. Prompt v2 and memory lessons lower the mistake rates while the random draws stay
the same, so A/B comparisons are paired. These numbers test the machinery, not model quality.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Callable

from ..domain import constants as C
from ..reliability.idempotency import stable_int
from .base import LLMRequest, LLMResponse, estimate_tokens


# how a fake model behaves: speed, how often it makes each kind of mistake, how the judge scores
@dataclass(frozen=True)
class MockProfile:
    ttft_ms: float
    tokens_per_s: float
    dfm_slip: float  # per closed-form rule, first draft
    design_slip: float  # concept-level packaging / CMF budget mistakes
    revise_skill: float  # probability a reviser fixes each listed issue
    judge_bias: float
    judge_noise: float
    diversity: float


# pro: slower, more careful. flash: faster, sloppier, more varied
PROFILES: dict[str, MockProfile] = {
    "deepseek-v4-pro": MockProfile(1300, 35, 0.08, 0.25, 0.90, -0.10, 0.40, 0.75),
    "deepseek-flash": MockProfile(500, 70, 0.11, 0.30, 0.80, 0.15, 0.50, 0.90),
    "gpt-4.1-mini": MockProfile(450, 80, 0.13, 0.32, 0.78, 0.05, 0.40, 0.85),
}
DEFAULT_PROFILE = PROFILES["deepseek-flash"]



# contract fields that change what a design should be; everything else is bookkeeping
_SEED_CONTRACT_FIELDS = ("brief_id", "intent", "cost_ceiling_cny", "max_envelope_mm", "max_mass_g",
                         "allowed_materials", "ip_rating")


class MockLLM:
    provider = "mock"

    def __init__(self, time_scale: float = 0.002):
        self.time_scale = time_scale
        # one fake 'model' function per agent role
        self._handlers: dict[str, Callable[[dict, random.Random, MockProfile], dict]] = {
            "brief_interpreter": interpret_brief,
            "concept_generator": generate_concept,
            "structural_modeler": build_structure,
            "reviser": revise,
            "quality_judge": judge,
            "explainer": explain,
        }

    async def complete(self, req: LLMRequest) -> LLMResponse:
        profile = PROFILES.get(req.model, DEFAULT_PROFILE)
        # Common random numbers: lessons and prompt version change *probabilities*, not the random draws, so
        # A/B comparisons in mock mode are paired and the deltas are not swamped by reseeding noise.
        seed_payload = {k: v for k, v in req.payload.items() if k not in ("lessons", "prompt_version")}
        # Seed only from the contract fields a design depends on, so adding bookkeeping fields to the contract
        # (assumptions, conflict notes, ...) doesn't redraw every random number and move the mock pass rate.
        if isinstance(seed_payload.get("contract"), dict):
            seed_payload["contract"] = {k: seed_payload["contract"].get(k) for k in _SEED_CONTRACT_FIELDS}
        rng = random.Random(stable_int(req.model, req.role, req.variant, seed_payload))
        handler = self._handlers.get(req.role)
        if handler is None:
            raise ValueError(f"mock has no behaviour for role '{req.role}'")
        # build the answer from the structured payload, not from the prompt text
        out = handler(req.payload, rng, profile)
        text = json.dumps(out)
        prompt_tokens = estimate_tokens("".join(m["content"] for m in req.messages))
        completion_tokens = estimate_tokens(text)
        # pretend latency: time to first token + output tokens / speed
        latency_ms = profile.ttft_ms + completion_tokens / profile.tokens_per_s * 1000
        # only sleep a tiny fraction of it so the demo stays fast
        await asyncio.sleep(latency_ms / 1000 * self.time_scale)
        return LLMResponse(text=text, model=req.model, provider=self.provider, prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens, latency_ms=round(latency_ms, 1))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# rule ids that the retrieved lessons talk about
def _lesson_rules(payload: dict) -> set[str]:
    return {l.get("rule_id") for l in payload.get("lessons", []) if l.get("rule_id")}


# should the model get this rule wrong this time?
def _slip(rng: random.Random, base: float, payload: dict, rule: str) -> bool:
    p = base
    if payload.get("prompt_version") == "v2":
        p *= 0.35  # explicit numeric DFM checklist in the prompt
    if rule in _lesson_rules(payload):
        p *= 0.3  # the model follows retrieved lessons most of the time
    return rng.random() < p


# highest-weighted priority, e.g. 'cost' for B1
def _dominant(priorities: dict) -> str:
    return max(priorities, key=lambda k: priorities[k])


# very rough 'reading' of numbers in the brief
_MONEY = re.compile(r"(?:¥|CNY|RMB)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:CNY|RMB|yuan|元)", re.I)
_THICK = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:thick|thin|deep|profile|depth)", re.I)
_MASS = re.compile(r"(?:under|below|max(?:imum)?|at most|<=?)\s*(\d+(?:\.\d+)?)\s*g\b", re.I)
_IP = re.compile(r"\bIP\s?(54|65|67)\b", re.I)
_DROP = re.compile(r"(\d+(?:\.\d+)?)\s*m\s*drop|drop[^.]{0,30}?(\d+(?:\.\d+)?)\s*m\b", re.I)


# ---------------------------------------------------------------------------
# brief interpreter
# ---------------------------------------------------------------------------
def interpret_brief(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    brief = payload["brief"]
    text = brief["text"]
    t = text.lower()

    # 0.25 base, +0.3 per keyword hit, capped at 1.0
    def score(words: tuple[str, ...]) -> float:
        hits = sum(1 for w in words if re.search(r"\b" + re.escape(w), t))  # word-prefix, not substring
        return round(min(1.0, 0.25 + 0.3 * hits), 2)

    pri = {
        "cost": score(("cost", "cheap", "budget", "afford", "price", "lowest", "economy", "retail")),
        "durability": score(("rugged", "outdoor", "drop", "construction", "site", "waterproof", "dust", "ip6", "tough")),
        "aesthetics": score(("premium", "living room", "home", "elegant", "minimal", "furniture", "design-led",
                             "beautiful", "soft-touch")),
        "sustainability": score(("recycled", "sustainab", "circular", "carbon", "eco", "repairab", "disassembl")),
    }
    dom = _dominant(pri)
    outdoor = pri["durability"] >= 0.55
    env = "outdoor" if outdoor and "indoor" not in t else ("mixed" if outdoor else "indoor")

    # materials the brief names explicitly win over the defaults below
    mentioned = [m for m, keys in (("PC-ABS", ("pc-abs", "pc/abs")), ("rABS", ("recycled abs", "rabs", "recycled")),
                                   ("ABS", (" abs",))) if any(k in t for k in keys)]
    if "only recycled" in t or "100% recycled" in t or "recycled only" in t:
        allowed = ["rABS"]
    elif mentioned:
        allowed = mentioned
    elif dom == "durability":
        allowed = ["PC-ABS", "ABS"]
    elif dom == "aesthetics":
        allowed = ["PC-ABS", "ABS"]
    elif dom == "sustainability":
        allowed = ["rABS"]
    else:
        allowed = ["ABS", "rABS", "PC-ABS"]

    finishes = {
        "durability": ["textured_heavy", "textured_light"],
        "aesthetics": ["smooth_matte", "smooth_gloss"],
        "sustainability": ["textured_light", "smooth_matte"],
        "cost": ["smooth_matte", "textured_light"],
    }[dom]

    m = _MONEY.search(text)
    cost = float(m.group(1) or m.group(2)) if m else None
    m = _THICK.search(text)
    thick = float(m.group(1)) if m else None
    m = _MASS.search(text)
    mass = float(m.group(1)) if m else None
    m = _IP.search(text)
    ip = f"IP{m.group(1)}" if m else ("IP54" if "splash" in t else "none")
    m = _DROP.search(text)
    drop = float(m.group(1) or m.group(2)) if m else None

    # spot the obvious conflicts a decent model would point out
    conflicts, questions = [], []
    if cost is not None and cost < 11 and (pri["aesthetics"] >= 0.55 or ip in ("IP65", "IP67")):
        conflicts.append({"conflict_id": "C1", "severity": "hard",
                          "description": f"Target CNY {cost:g}/unit is unrealistic with premium finish or sealing",
                          "requirements": ["unit cost", "finish/sealing"],
                          "proposed_resolution": "Raise the target to the CNY 18 ceiling or drop sealing/paint"})
    if ip in ("IP65", "IP67") and thick is not None and thick < 26:
        conflicts.append({"conflict_id": "C2", "severity": "soft",
                          "description": "Sealed gasket housing at this slimness leaves no room for the gasket land",
                          "requirements": ["ingress rating", "thickness"],
                          "proposed_resolution": "Relax thickness to the minimum feasible stack height"})
    if pri["sustainability"] >= 0.55 and any(w in t for w in ("paint", "soft-touch", "soft touch")):
        conflicts.append({"conflict_id": "C3", "severity": "soft",
                          "description": "Paint / soft-touch coatings contaminate the recycling stream",
                          "requirements": ["recycled content", "coated finish"],
                          "proposed_resolution": "Use moulded-in colour with texture instead of coatings"})
    if cost is None:
        questions.append({"question_id": "Q1", "question": "Is there a unit cost target below the CNY 18 ceiling?",
                          "default_assumption": "Use the CNY 18 housing BOM ceiling", "blocking": False})
    if conflicts:
        questions.append({"question_id": "Q2", "question": "Which requirement wins when they conflict?",
                          "default_assumption": "Manufacturability and budget win; aesthetics adapts",
                          "blocking": any(c["severity"] == "hard" for c in conflicts)})

    keywords = [w for w in ("rugged", "premium", "minimal", "soft", "recycled", "compact", "friendly", "technical",
                            "warm", "durable", "calm", "honest") if w in t][:6] or ["clean", "compact"]
    must = [s.strip() for s in re.findall(r"must ([^.;]+)", text, re.I)][:5]
    return {
        "brief_id": brief["brief_id"],
        "summary": text[:300] if len(text) >= 10 else "Portable air-quality detector housing",
        "priorities": pri, "environment": env, "allowed_materials": allowed, "preferred_finishes": finishes,
        "style_keywords": keywords, "target_unit_cost_cny": cost, "max_thickness_mm": thick, "max_mass_g": mass,
        "ip_rating": ip, "drop_height_m": drop, "must_haves": must, "conflicts": conflicts,
        "open_questions": questions,
    }


# ---------------------------------------------------------------------------
# concept generator
# ---------------------------------------------------------------------------
_PALETTES = {
    "durability": ["safety orange / graphite", "olive drab", "hi-vis yellow / black", "slate grey"],
    "aesthetics": ["warm white", "sand beige", "sage green", "charcoal"],
    "sustainability": ["speckled grey (recycled)", "natural flax", "forest green"],
    "cost": ["light grey", "white", "black"],
}


def generate_concept(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    contract = payload["contract"]
    intent = contract["intent"]
    direction = payload["direction"]
    pri = intent["priorities"]
    dom = _dominant(pri)
    v2 = payload.get("prompt_version") == "v2"
    # v2 shows a reference design; most of the time the model just copies it
    collapse = v2 and rng.random() < 0.85  # few-shot example dominates
    diversity = prof.diversity * (0.5 if v2 else 1.0)

    silhouette = "rounded_rect" if collapse else direction["silhouette"]
    allowed = contract["allowed_materials"]
    pref = {"durability": "PC-ABS", "sustainability": "rABS", "cost": "ABS", "aesthetics": "PC-ABS"}[dom]
    if collapse or rng.random() > diversity:
        material = pref if pref in allowed else allowed[0]
    else:
        material = rng.choice(allowed)
    finish = intent["preferred_finishes"][0] if collapse else rng.choice(intent["preferred_finishes"])
    color = _PALETTES[dom][0] if collapse else rng.choice(_PALETTES[dom])

    # decoration depends on what the brief cares about
    ops: list[str] = []
    if pri["aesthetics"] >= 0.55:
        ops.append(rng.choice(["soft_touch", "paint", "soft_touch", "iml"]))
        if rng.random() < 0.5:
            ops.append("laser_etch")
    if pri["durability"] >= 0.55 and (silhouette == "bumpered" or rng.random() < 0.45):
        ops.append("tpe_overmold")
    if pri["sustainability"] >= 0.55:
        ops = [o for o in ops if o not in ("paint", "soft_touch", "iml")] + ["laser_etch"]
    if pri["cost"] >= 0.55 and not ops:
        ops.append("pad_print")
    # design mistake: piles on decoration and blows the budget
    if not v2 and pri["aesthetics"] >= 0.55 and rng.random() < prof.design_slip:
        ops.append("iml")  # stacking decoration -> budget overrun
    ops = sorted(set(ops))

    max_h = contract["max_envelope_mm"]["height"]
    slim = direction.get("slim", False) or pri["aesthetics"] >= 0.55
    # design mistake: too thin to fit the battery
    if slim and not v2 and rng.random() < prof.design_slip:
        height = round(rng.uniform(22.5, 24.5), 1)  # looks great, cannot fit the battery stack
    elif slim:
        height = round(rng.uniform(25.5, 27.0), 1)
    else:
        height = round(rng.uniform(26.5, min(29.5, max_h)), 1)
    length = round(rng.uniform(108, 124), 0) if not collapse else 116.0
    width = round(rng.uniform(60, 72), 0) if not collapse else 66.0
    # design mistake: max-size box, which later runs over weight
    if pri["durability"] >= 0.55 and not v2 and rng.random() < prof.design_slip:
        length, width, height = 125.0, 75.0, min(30.0, max_h)  # max envelope + bumper -> mass overrun
    radius = {"pebble": 18, "capsule": 22, "bumpered": 10, "chamfered_slab": 3, "rounded_rect": 8}[silhouette]

    intake_face = direction.get("intake_face", "front")
    display_w = 44.0 if not _slip(rng, prof.dfm_slip * 0.5, payload, "DISPLAY_WINDOW_MIN") else 40.0
    # intent anchors: the decisions the 3D step must not lose
    anchors = [
        {"anchor_id": "IA1", "statement": f"{silhouette.replace('_', ' ')} silhouette, {radius} mm corner radius",
         "category": "form"},
        {"anchor_id": "IA2", "statement": "Display upper-centre on the front face, readable at arm's length",
         "category": "usability"},
        {"anchor_id": "IA3", "statement": f"{finish.replace('_', ' ')} {color} in {material}"
                                           + (f" with {', '.join(ops)}" if ops else ""), "category": "cmf"},
    ]
    if pri["durability"] >= 0.55:
        anchors.append({"anchor_id": "IA4", "statement": "Protected corners survive a drop onto concrete",
                        "category": "durability"})
    if pri["sustainability"] >= 0.55:
        anchors.append({"anchor_id": "IA5", "statement": "Mono-material housing, separable without tools",
                        "category": "sustainability"})
    if pri["cost"] >= 0.55:
        anchors.append({"anchor_id": "IA6", "statement": "Two-part shell, no side actions, minimal operations",
                        "category": "cost"})
    name = f"{direction['name'].replace('_', ' ').title()} {material}"
    return {
        "variant_id": payload["variant_id"], "brief_id": contract["brief_id"], "direction": direction["name"],
        "name": name[:60],
        "form_language": f"{direction['name'].replace('_', ' ')} form; {', '.join(intent['style_keywords'][:3])} "
                         f"character with {finish.replace('_', ' ')} surfaces",
        "silhouette": silhouette,
        "envelope_mm": {"length": length, "width": width, "height": height},
        "corner_radius_mm": radius,
        "display": {"face": "front", "x_pct": 50, "y_pct": round(rng.uniform(28, 40), 0)},
        "display_window_mm": {"width": display_w, "height": 20.0},
        "intake": {"face": intake_face, "x_pct": 50, "y_pct": 78 if intake_face == "front" else 50},
        "usb": {"face": "bottom", "x_pct": 50, "y_pct": 50},
        "cmf": {"material": material, "finish": finish, "color": color, "secondary_ops": ops},
        "intent_anchors": anchors,
        "rationale": f"Direction '{direction['name']}' answers the {dom}-led brief: "
                     f"{intent['summary'][:160]}",
    }


# ---------------------------------------------------------------------------
# structural modeler
# ---------------------------------------------------------------------------
# which spec feature each kind of anchor usually maps to
_ANCHOR_FEATURES = {
    "form": ["envelope"], "usability": ["display_window"], "cmf": ["front_housing.finish"],
    "durability": ["rear_housing", "front_housing_rib"], "sustainability": ["front_housing.material", "closure"],
    "cost": ["closure", "parting"],
}


def build_structure(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    concept, contract = payload["concept"], payload["contract"]
    pri = contract["intent"]["priorities"]
    cmf = concept["cmf"]
    textured = cmf["finish"].startswith("textured")
    # thicker walls for rugged, thinner for premium or cheap
    wall = 2.5 if pri["durability"] >= 0.55 else (1.8 if pri["aesthetics"] >= 0.55 or pri["cost"] >= 0.55 else 2.0)

    def slip(rule: str) -> bool:
        return _slip(rng, prof.dfm_slip, payload, rule)

    # each block below may 'slip' on one rule, like a real first draft
    min_wall, max_wall = wall, wall
    if slip("WALL_RANGE"):
        min_wall = wall = 1.3 if wall < 2.2 else wall
        max_wall = max(wall, 3.2 if wall >= 2.2 else wall)
    draft_textured = 2.5 if textured else 1.5
    draft_slipped = slip("DRAFT_MIN")
    rib_t = round(wall * (0.7 if slip("RIB_RATIO") else 0.5), 2)
    inner_r = round(wall * (0.3 if slip("INNER_RADIUS") else 0.6), 2)

    # closure follows the brief: snap-fits for easy recycling, screws for rugged
    if pri["sustainability"] >= 0.55:
        closure, screws = "snap_fits", 0
    elif pri["aesthetics"] >= 0.55:
        closure, screws = "screws_and_snaps", 2
    elif pri["durability"] >= 0.55:
        closure, screws = "screws", 6
    else:
        closure, screws = "screws", 4
    undercuts = []
    if closure in ("snap_fits", "screws_and_snaps"):
        n = 4 if closure == "snap_fits" else 2
        for i in range(n):
            declared = not slip("UNDERCUT_DECLARED")
            undercuts.append({"undercut_id": f"snap_{i + 1}", "feature": "snap-fit hook on rear housing lip",
                              "mechanism": "lifter" if declared else None,
                              "side_action_cost_cny": 9000.0 if declared else None})

    ops = list(cmf["secondary_ops"])
    front_ops = [o for o in ops if o != "tpe_overmold"]
    rear_ops = [o for o in ops if o == "tpe_overmold"]

    # draft angles per face; a slip copies the smooth value onto a textured face
    def drafts() -> list[dict]:
        d = draft_textured
        out = [{"face": "outer_sides", "draft_deg": d, "textured": textured},
               {"face": "inner_sides", "draft_deg": 1.5, "textured": False},
               {"face": "ribs", "draft_deg": 1.0, "textured": False}]
        if draft_slipped and textured:
            out[0]["draft_deg"] = 1.0  # copied the smooth-face value onto a textured face
        elif draft_slipped:
            out[0]["draft_deg"] = 0.5
        return out

    def part(pid: str, role: str, part_ops: list[str]) -> dict:
        return {
            "part_id": pid, "role": role, "material": cmf["material"], "finish": cmf["finish"],
            "nominal_wall_mm": wall, "min_wall_mm": min_wall, "max_wall_mm": max_wall, "inner_radius_mm": inner_r,
            "drafts": drafts(),
            "ribs": [{"rib_id": f"{pid}_rib", "thickness_mm": rib_t, "height_mm": 6.0, "length_mm": 40.0, "count": 4}],
            "bosses": [{"boss_id": f"{pid}_boss", "outer_d_mm": 5.0, "inner_d_mm": 2.5, "height_mm": 8.0,
                        "count": max(2, screws)}] if screws else [],
            "undercuts": undercuts if role == "rear_housing" else [],
            "secondary_ops": part_ops,
        }

    parts = [part("front_housing", "front_housing", front_ops), part("rear_housing", "rear_housing", rear_ops)]
    if concept["silhouette"] == "bumpered" or (pri["durability"] >= 0.55 and rng.random() < prof.design_slip):
        parts.append(part("bumper", "bumper", []))

    vent = {"face": concept["intake"]["face"], "zone_w_mm": 18.0, "zone_h_mm": 18.0, "slot_count": 6,
            "slot_w_mm": 1.2, "slot_l_mm": 14.0}
    if slip("VENT_OPEN_AREA"):
        vent.update(slot_count=3, slot_w_mm=1.0, slot_l_mm=12.0)
    usb = {"width": 10.0, "height": 4.0} if slip("USB_CLEARANCE") else {"width": 11.0, "height": 5.0}

    ip = contract["ip_rating"]
    sealing = "gasket_and_membrane_vent" if ip in ("IP65", "IP67") else ("gasket" if ip == "IP54" else "none")
    trace = []
    for a in concept["intent_anchors"]:
        feats = list(_ANCHOR_FEATURES[a["category"]])
        if a["category"] == "durability" and len(parts) == 3:
            feats = ["bumper"]
        trace.append({"anchor_id": a["anchor_id"], "realized_by": feats, "note": a["statement"][:80]})
    # sometimes drop an anchor, i.e. intent lost between 2D and 3D
    if trace and _slip(rng, prof.dfm_slip * 0.4, payload, "INTENT_TRACE_COMPLETE"):
        trace.pop()  # intent silently dropped at the 2D -> 3D handoff

    return {
        "variant_id": concept["variant_id"], "brief_id": concept["brief_id"], "concept_name": concept["name"],
        "revision": 0, "envelope_mm": concept["envelope_mm"], "parting": "front_back", "parts": parts,
        "display_window_mm": concept["display_window_mm"], "vent": vent, "usb_opening_mm": usb,
        "internal_layout": "stacked", "closure": closure, "screw_count": screws, "sealing": sealing,
        "intent_trace": trace, "change_log": ["initial structural draft from concept"],
    }


# ---------------------------------------------------------------------------
# reviser (tool-using)
# ---------------------------------------------------------------------------
# cheapest material first / most expensive decoration first
_CHEAP_ORDER = ["ABS", "rABS", "PC-ABS"]
_OP_COST_ORDER = ["soft_touch", "paint", "tpe_overmold", "iml", "pad_print", "laser_etch"]


def revise(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    spec = json.loads(json.dumps(payload["structure"]))
    contract, concept = payload["contract"], payload["concept"]
    failures = {f["rule_id"] for f in payload["failures"]}
    cost = payload.get("cost")
    over_budget = cost is not None and not cost["within_budget"]
    tool_results = payload.get("tool_results", [])

    # over budget: price an option with the tool before changing anything
    if over_budget and not tool_results:
        allowed = [m for m in _CHEAP_ORDER if m in contract["allowed_materials"]]
        return {"step": {"action": "tool_call", "tool": "cost_what_if",
                         "args": {"material": allowed[0], "drop_secondary_ops": _expensive_ops(spec)[:1],
                                  "remove_bumper": True},
                         "reason": "estimate saving from material swap and dropping the costliest operation"}}
    if "MASS_MAX" in failures and not tool_results and rng.random() < 0.5:
        return {"step": {"action": "tool_call", "tool": "materials_lookup",
                         "args": {"material": spec["parts"][0]["material"]},
                         "reason": "check density before resizing walls"}}

    # imperfect reviser: sometimes misses one issue, so another round is needed
    skip_one = rng.random() > prof.revise_skill
    todo = sorted(failures) + (["COST"] if over_budget else [])
    if skip_one and len(todo) > 1:
        todo.pop(rng.randrange(len(todo)))

    changes: list[str] = []
    for rule in todo:
        fix = _DESIGN_FIXES.get(rule)
        changes.extend(fix(spec, contract, concept) if fix else _closed_form_fix(spec, rule))

    if not changes:
        changes.append("no change possible within constraints")
    spec["revision"] = int(spec.get("revision", 0)) + 1
    spec["change_log"] = list(spec.get("change_log", [])) + changes
    return {"step": {"action": "final", "structure": spec, "changes": changes}}


# --- design-level fixes: each one trades something away and says what --------
def _fix_packaging(spec: dict, contract: dict, concept: dict) -> list[str]:
    # make the box tall enough for the PCB + battery; thin the walls if the max height won't allow it
    req_h = C.PCB.height + C.BATTERY.height + C.STACK_GAP_MM + 2 * C.PACKAGING_CLEARANCE_MM
    max_h = contract["max_envelope_mm"]["height"]
    parts = spec["parts"]
    wall = max(p["nominal_wall_mm"] for p in parts if p["role"] != "bumper")
    need = req_h + 2 * wall + 0.5
    if need > max_h:
        for p in parts:
            p["nominal_wall_mm"] = p["min_wall_mm"] = p["max_wall_mm"] = 1.8
        need = req_h + 2 * 1.8 + 0.5
    spec["envelope_mm"]["height"] = round(min(need, max_h), 1)
    spec["envelope_mm"]["length"] = max(spec["envelope_mm"]["length"], 98.0)
    return [f"raised envelope height to {spec['envelope_mm']['height']} mm to fit the battery stack"]


def _fix_envelope(spec: dict, contract: dict, concept: dict) -> list[str]:
    mx = contract["max_envelope_mm"]
    for k in ("length", "width", "height"):
        spec["envelope_mm"][k] = min(spec["envelope_mm"][k], mx[k])
    return ["clamped envelope to the brief's maximum"]


def _fix_mass(spec: dict, contract: dict, concept: dict) -> list[str]:
    changes = []
    if _remove_bumper(spec):
        changes.append("removed separate bumper part; corner protection moved into rear housing ribs")
    spec["envelope_mm"]["length"] = min(spec["envelope_mm"]["length"], 116.0)
    spec["envelope_mm"]["width"] = min(spec["envelope_mm"]["width"], 66.0)
    for p in spec["parts"]:
        if p["nominal_wall_mm"] > 2.2:
            p["nominal_wall_mm"] = p["min_wall_mm"] = p["max_wall_mm"] = 2.2
    return changes + ["shrank footprint to 116 x 66 mm and capped walls at 2.2 mm"]


def _fix_material(spec: dict, contract: dict, concept: dict) -> list[str]:
    allowed = contract["allowed_materials"]
    for p in spec["parts"]:
        if p["material"] not in allowed:
            p["material"] = allowed[0]
    return [f"switched material to {allowed[0]}"]


def _fix_intent_trace(spec: dict, contract: dict, concept: dict) -> list[str]:
    traced = {t["anchor_id"] for t in spec["intent_trace"]}
    for a in concept["intent_anchors"]:
        if a["anchor_id"] not in traced:
            spec["intent_trace"].append({"anchor_id": a["anchor_id"],
                                         "realized_by": _ANCHOR_FEATURES[a["category"]][:1],
                                         "note": "restored mapping"})
    return ["restored missing intent-anchor mappings"]


def _fix_cost(spec: dict, contract: dict, concept: dict) -> list[str]:
    # one step per round: decoration, then bumper, then snap-fits, then material
    ops = _expensive_ops(spec)
    if ops:
        for p in spec["parts"]:
            p["secondary_ops"] = [o for o in p["secondary_ops"] if o != ops[0]]
        return [f"dropped {ops[0]} to recover budget"]
    if _remove_bumper(spec):
        return ["removed bumper part to save its mould"]
    if any(p["undercuts"] for p in spec["parts"]):
        for p in spec["parts"]:
            p["undercuts"] = []
        spec["closure"], spec["screw_count"] = "screws", 4
        return ["replaced snap-fits (lifters) with 4 screws"]
    cheapest = next(m for m in _CHEAP_ORDER if m in contract["allowed_materials"])
    for p in spec["parts"]:
        p["material"] = cheapest
    return [f"switched to {cheapest} for cost"]


_DESIGN_FIXES = {
    "PACKAGING_FIT": _fix_packaging, "ENVELOPE_MAX": _fix_envelope, "MASS_MAX": _fix_mass,
    "MATERIAL_ALLOWED": _fix_material, "INTENT_TRACE_COMPLETE": _fix_intent_trace, "COST": _fix_cost,
}


def _remove_bumper(spec: dict) -> bool:
    """Drop the separate bumper part (if any) and point its intent traces at the rear shell."""
    if not any(p["role"] == "bumper" for p in spec["parts"]):
        return False
    spec["parts"] = [p for p in spec["parts"] if p["role"] != "bumper"]
    _retrace(spec, "bumper", "rear_housing")
    return True


def _expensive_ops(spec: dict) -> list[str]:
    present = {o for p in spec["parts"] for o in p["secondary_ops"]}
    return [o for o in _OP_COST_ORDER if o in present and o not in ("pad_print", "laser_etch")]


def _retrace(spec: dict, old: str, new: str) -> None:
    for t in spec["intent_trace"]:
        t["realized_by"] = [new if f == old else f for f in t["realized_by"]]


# simple numeric fixes (same ones the AutoFixer does)
def _closed_form_fix(spec: dict, rule: str) -> list[str]:
    for p in spec["parts"]:
        w = p["nominal_wall_mm"]
        if rule == "WALL_RANGE":
            w = min(max(w, 1.6), 2.8)
            p["nominal_wall_mm"] = p["min_wall_mm"] = p["max_wall_mm"] = w
        elif rule == "RIB_RATIO":
            for r in p["ribs"]:
                r["thickness_mm"] = round(0.5 * w, 2)
        elif rule == "INNER_RADIUS":
            p["inner_radius_mm"] = round(0.6 * w, 2)
        elif rule == "DRAFT_MIN":
            for d in p["drafts"]:
                d["draft_deg"] = max(d["draft_deg"], 2.5 if (d["textured"] or p["finish"].startswith("textured")) else 1.5)
        elif rule == "UNDERCUT_DECLARED":
            for u in p["undercuts"]:
                u["mechanism"] = u.get("mechanism") or "lifter"
                u["side_action_cost_cny"] = u.get("side_action_cost_cny") or 9000.0
    if rule == "VENT_OPEN_AREA":
        spec["vent"].update(zone_w_mm=20.0, zone_h_mm=20.0, slot_count=7, slot_w_mm=1.2, slot_l_mm=16.0)
    elif rule == "USB_CLEARANCE" or rule == "USB_OPENING_MIN":
        spec["usb_opening_mm"] = {"width": 11.0, "height": 5.0}
    elif rule == "DISPLAY_WINDOW_MIN":
        spec["display_window_mm"] = {"width": 44.0, "height": 20.0}
    elif rule == "SEALING_FOR_IP":
        spec["sealing"] = "gasket_and_membrane_vent"
    return [f"fixed {rule}"]


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------
def judge(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    contract, concept, spec = payload["contract"], payload["concept"], payload["structure"]
    cost = payload.get("cost") or {}
    intent = contract["intent"]
    pri = intent["priorities"]
    dom = _dominant(pri)
    parts = spec["parts"]
    mats = {p["material"] for p in parts}
    finish = parts[0]["finish"]
    ops = {o for p in parts for o in p["secondary_ops"]}
    textured = finish.startswith("textured")
    wall = max(p["nominal_wall_mm"] for p in parts)

    # four signals per brief type; how many are present drives brief_adherence
    if dom == "durability":
        feats = ["PC-ABS" in mats, textured, wall >= 2.2, spec["sealing"] != "none" or "tpe_overmold" in ops]
    elif dom == "aesthetics":
        feats = [not textured, bool(ops & {"soft_touch", "paint", "iml"}), spec["envelope_mm"]["height"] <= 27.5,
                 spec["closure"] != "screws"]
    elif dom == "sustainability":
        feats = [mats == {"rABS"}, not (ops & {"paint", "soft_touch", "iml"}), len(mats) == 1,
                 spec["closure"] in ("snap_fits", "screws")]
    else:
        tgt = intent.get("target_unit_cost_cny") or contract["cost_ceiling_cny"]
        feats = [cost.get("total_cny", 99) <= tgt, mats <= {"ABS", "rABS"}, not (ops - {"pad_print", "laser_etch"}),
                 not any(p["undercuts"] for p in parts)]
    adherence = {4: 5, 3: 3, 2: 2}.get(sum(feats), 1)  # a judge should not call 3-of-4 "adherent"

    # add this judge's bias and random noise, keep it in 1..5
    def noisy(x: float) -> int:
        return int(min(5, max(1, round(x + prof.judge_bias + rng.gauss(0, prof.judge_noise)))))

    # count revisions that gave something up (dropped/removed/switched)
    lost = sum(1 for c in spec.get("change_log", []) if c.startswith(("dropped", "removed", "switched")))
    usability = 4.0 - (1.5 if concept["intake"]["face"] == "bottom" else 0) - (0.8 if concept["display"]["y_pct"] > 60 else 0)
    form = 3.3 + (0.7 if concept["silhouette"] in ("pebble", "capsule", "chamfered_slab") else 0.0)
    if concept["name"].startswith("Fallback"):
        form -= 1.2
    if dom == "aesthetics" and concept["silhouette"] == "bumpered":
        form -= 1.0  # a bumper reads as a tool, not a furniture object
    if dom == "durability" and concept["silhouette"] in ("pebble", "capsule"):
        form -= 0.6
    if intent["environment"] == "indoor" and concept["intake"]["face"] == "back":
        usability -= 0.7  # desk units sit against a wall
    cmf = 3.2 + (0.8 if finish in intent["preferred_finishes"][:1] else 0.0) + (0.3 if len(mats) == 1 else -0.3)
    scores = {
        "brief_adherence": noisy(adherence), "form_coherence": noisy(form), "cmf_fit": noisy(cmf),
        "usability": noisy(usability), "intent_preservation": noisy(4.8 - 0.8 * lost),
    }
    overall = (scores["brief_adherence"] * 2 + sum(v for k, v in scores.items() if k != "brief_adherence")) / 6
    verdict = "accept" if overall >= 3.5 else ("weak" if overall >= 2.8 else "reject")
    flags = []
    if lost:
        flags.append(f"{lost} intent-affecting change(s) during revision")
    if adherence <= 2:
        flags.append(f"weak fit to the {dom}-led brief")
    return {"scores": scores, "verdict": verdict, "flags": flags,
            "rationale": f"{dom}-led brief; {sum(feats)}/4 key brief signals present in the spec "
                         f"({concept['silhouette']}, {finish}, {'/'.join(sorted(mats))}, ops={sorted(ops) or 'none'})."}


# ---------------------------------------------------------------------------
# explainer ("ask" feature)
# ---------------------------------------------------------------------------
# Ask feature: just stitch the retrieved facts into an answer
def explain(payload: dict, rng: random.Random, prof: MockProfile) -> dict:
    facts: list[str] = payload.get("facts", [])
    cited = sorted({m for f in facts for m in re.findall(r"\b[\w-]+-v\d{3}\b", f)})[:6]
    if not facts:
        return {"answer": "I could not find records in this run that match the question.", "cited_variants": []}
    body = "; ".join(facts[:5])
    return {"answer": f"From the run record: {body}", "cited_variants": cited}
