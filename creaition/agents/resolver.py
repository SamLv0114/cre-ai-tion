"""Constraint resolver and the Ask gate. Plain code, no LLM.

Merges the interpreted brief with the hard product limits, checks the model's numbers against a regex
pass, and uses arithmetic (cheapest / thinnest / lightest possible housing) to spot briefs that can't be
met. The Ask gate puts conflicts to the designer, or records the defaults in batch mode.
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Optional

from ..domain import baseline, constants as C, extract
from ..domain.schemas import (
    ConflictNote, DesignBrief, DesignContract, DesignIntent, Dims3, OpenQuestion, Relaxation,
)
from ..observability.tracing import Tracer

_IP_ORDER = {"none": 0, "IP54": 1, "IP65": 2, "IP67": 3}


# the tighter of two limits (lower cost, thinner, lighter)
def _stricter(a: Optional[float], b: Optional[float]) -> Optional[float]:
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


# relax only as far as needed: the floor plus room for the brief's finishes, rounded up, never past the platform
def _relaxed(floor: float, platform_limit: float) -> float:
    return min(platform_limit, math.ceil(floor * (1 + C.RELAX_HEADROOM)))


def resolve(brief: DesignBrief, intent: DesignIntent) -> DesignContract:
    nc = extract.extract(brief.text)
    conflicts = list(intent.conflicts)
    questions = list(intent.open_questions)

    # 1) cross-check numbers extracted by the model against the deterministic pass
    checked: dict[str, Optional[float]] = {}
    platform = {"target_unit_cost_cny": C.BOM_CEILING_CNY, "max_thickness_mm": C.ENVELOPE_MAX.height,
                "max_mass_g": C.PRODUCT_MASS_MAX_G}
    for name, llm_v, rx_v in (("target_unit_cost_cny", intent.target_unit_cost_cny, nc.cost_cny),
                              ("max_thickness_mm", intent.max_thickness_mm, nc.thickness_mm),
                              ("max_mass_g", intent.max_mass_g, nc.mass_g)):
        if rx_v is None and llm_v is not None and llm_v >= platform[name]:
            # The model restated the platform-wide limit (seen live: "18" when the brief names no target).
            # Not a brief-specific constraint and not a disagreement; the platform limit applies anyway.
            checked[name] = None
            continue
        # model and regex disagree: ask, and use the stricter value meanwhile
        if (llm_v is None) != (rx_v is None) or (llm_v is not None and abs(llm_v - rx_v) > 1e-6):
            chosen = _stricter(llm_v, rx_v)
            questions.append(OpenQuestion(
                question_id=f"QX-{name}", blocking=False,
                question=f"The text reads {name}={rx_v} but the interpretation says {llm_v}. Which is intended?",
                default_assumption=f"use the stricter value {chosen}"))
            checked[name] = chosen
        else:
            checked[name] = llm_v
    ip = intent.ip_rating
    # same idea for the IP rating: keep the higher one
    if nc.ip and _IP_ORDER[nc.ip] > _IP_ORDER[ip]:
        questions.append(OpenQuestion(question_id="QX-ip", question=f"Text mentions {nc.ip}; interpretation says {ip}.",
                                      default_assumption=f"use {nc.ip}"))
        ip = nc.ip

    target = checked["target_unit_cost_cny"]
    # the brief can only tighten the platform limits, never loosen them
    ceiling = min(C.BOM_CEILING_CNY, target) if target else C.BOM_CEILING_CNY
    max_h = min(C.ENVELOPE_MAX.height, checked["max_thickness_mm"]) if checked["max_thickness_mm"] else C.ENVELOPE_MAX.height
    max_mass = min(C.PRODUCT_MASS_MAX_G, checked["max_mass_g"]) if checked["max_mass_g"] else C.PRODUCT_MASS_MAX_G
    allowed = list(intent.allowed_materials)

    # 2) feasibility by arithmetic. A brief limit can be relaxed back toward the platform limit, never past it.
    relax: list[Relaxation] = []
    blockers: list[str] = []
    floor_cost = baseline.floor_cost_cny(allowed, ip)
    if floor_cost > ceiling:
        reason = f"cheapest feasible housing costs CNY {floor_cost:.2f} > target CNY {ceiling:g}"
        if floor_cost <= C.BOM_CEILING_CNY:
            relax.append(Relaxation(field="cost_ceiling_cny", strict_value=ceiling,
                                    relaxed_value=_relaxed(floor_cost, C.BOM_CEILING_CNY), reason=reason))
        else:
            blockers.append(f"cheapest feasible housing costs CNY {floor_cost:.2f}, above the platform ceiling "
                            f"of CNY {C.BOM_CEILING_CNY:g}")
    min_h = baseline.min_feasible_height_mm()
    if max_h < min_h:
        reason = f"PCB+battery stack needs >= {min_h:.1f} mm overall height at 1.5 mm walls"
        if min_h <= C.ENVELOPE_MAX.height:
            relax.append(Relaxation(field="max_height_mm", strict_value=max_h,
                                    relaxed_value=min(C.ENVELOPE_MAX.height, math.ceil(min_h + 0.5)), reason=reason))
        else:
            blockers.append(f"{reason}, above the platform maximum of {C.ENVELOPE_MAX.height:g} mm")
    floor_mass = baseline.floor_mass_g(allowed, ip)
    if floor_mass > max_mass:
        reason = f"lightest feasible product weighs {floor_mass:.1f} g > {max_mass:g} g"
        if floor_mass <= C.PRODUCT_MASS_MAX_G:
            relax.append(Relaxation(field="max_mass_g", strict_value=max_mass,
                                    relaxed_value=_relaxed(floor_mass, C.PRODUCT_MASS_MAX_G), reason=reason))
        else:
            blockers.append(f"lightest feasible product weighs {floor_mass:.1f} g, above the platform limit "
                            f"of {C.PRODUCT_MASS_MAX_G:g} g")
    # every relaxation and every blocker is also a hard conflict
    for i, r in enumerate(relax, 1):
        conflicts.append(ConflictNote(conflict_id=f"R{i}", source="rule", severity="hard",
                                      description=f"Infeasible as written: {r.reason}",
                                      requirements=[r.field], proposed_resolution=f"relax {r.field} to {r.relaxed_value:g}"))
    for i, b in enumerate(blockers, 1):
        conflicts.append(ConflictNote(conflict_id=f"P{i}", source="rule", severity="hard",
                                      description=f"Impossible within platform limits: {b}",
                                      proposed_resolution="cannot be relaxed; the product requirements must change"))
    # 3) requests the platform can't make at all (metal housing, CNC, 4 mm walls...). Variants will stay inside
    # the platform limits, so say so up front instead of letting the brief be quietly reinterpreted.
    for i, item in enumerate(extract.out_of_platform(brief.text), 1):
        conflicts.append(ConflictNote(
            conflict_id=f"X{i}", source="rule", severity="hard", requirements=[item],
            description=f"Brief asks for {item}; the platform only makes injection-moulded ABS / PC-ABS / rABS "
                        f"housings with {C.WALL_MIN_MM}-{C.WALL_MAX_MM} mm walls",
            proposed_resolution="design the closest version within platform limits; designer to confirm"))
    if ip in ("IP65", "IP67"):
        conflicts.append(ConflictNote(conflict_id="R-vent", source="rule", severity="soft",
                                      description=f"{ip} sealing vs. >= 15% open intake",
                                      requirements=["ingress rating", "vent open area"],
                                      proposed_resolution="gasket + ePTFE membrane vent (adds ~CNY 2.7/unit)"))

    contract = DesignContract(
        brief_id=brief.brief_id, intent=intent, cost_ceiling_cny=ceiling,
        max_envelope_mm=Dims3(length=C.ENVELOPE_MAX.length, width=C.ENVELOPE_MAX.width, height=max_h),
        max_mass_g=max_mass, allowed_materials=allowed, ip_rating=ip, floor_cost_cny=floor_cost,
        conflicts=conflicts, open_questions=questions, relaxations=relax, platform_blockers=blockers,
    )
    return apply_decisions(contract)


def apply_decisions(contract: DesignContract, answers: Optional[dict[str, str]] = None) -> DesignContract:
    """Recompute effective limits from accepted relaxations and record every assumption made."""
    answers = answers or {}
    ceiling, mass, height = contract.cost_ceiling_cny, contract.max_mass_g, contract.max_envelope_mm.height
    assumptions: list[str] = []
    # use the relaxed or the strict value depending on what the designer chose
    for r in contract.relaxations:
        if r.field == "cost_ceiling_cny":
            ceiling = r.relaxed_value if r.accepted else r.strict_value
        elif r.field == "max_mass_g":
            mass = r.relaxed_value if r.accepted else r.strict_value
        elif r.field == "max_height_mm":
            height = r.relaxed_value if r.accepted else r.strict_value
        verb = "Relaxed" if r.accepted else "Kept (designer refused relaxation)"
        assumptions.append(f"{verb} {r.field}: {r.strict_value:g} -> {r.relaxed_value if r.accepted else r.strict_value:g} "
                           f"({r.reason})")
    for q in contract.open_questions:
        if q.question_id in answers:
            assumptions.append(f"{q.question_id} answered by designer: {answers[q.question_id]}")
        else:
            assumptions.append(f"{q.question_id} default: {q.default_assumption}")
    # still impossible: platform blockers, plus any relaxation the designer said no to
    infeasible = list(contract.platform_blockers)
    infeasible += [f"designer kept {r.field} at {r.strict_value:g}, but {r.reason}"
                   for r in contract.relaxations if not r.accepted]
    # any relaxation, conflict or unanswered blocking question sends every variant to a human
    blocking = any(q.blocking and q.question_id not in answers for q in contract.open_questions)
    return contract.model_copy(update={
        "cost_ceiling_cny": ceiling, "max_mass_g": mass,
        "max_envelope_mm": contract.max_envelope_mm.model_copy(update={"height": height}),
        "assumptions": assumptions, "infeasible_reasons": infeasible,
        "requires_human_resolution": bool(contract.relaxations) or blocking or contract.conflict_detected,
    })


def _safe_input(prompt: str) -> str:
    try:
        reply = input(prompt)
    except EOFError:  # piped / closed stdin -> take the default
        reply = ""
    if not sys.stdin.isatty():  # echo scripted answers so logs show the full dialogue
        print(reply or "(default)")
    return reply


def clarify(contract: DesignContract, tracer: Tracer, *, interactive: bool,
            ask: Callable[[str], str] = _safe_input) -> DesignContract:
    """The Ask feature: put conflicts and open questions to the designer (or adopt defaults in batch)."""
    if not contract.relaxations and not contract.open_questions:
        return contract
    # batch mode: log the questions and keep the defaults
    if not interactive:
        for r in contract.relaxations:
            tracer.event("ask", f"[batch default] {r.reason} -> relax {r.field} to {r.relaxed_value:g}")
        for q in contract.open_questions:
            tracer.event("ask", f"[batch default] {q.question} -> {q.default_assumption}")
        return contract
    relax = []
    for r in contract.relaxations:
        # interactive: y/n per relaxation, then free-text answers to the questions
        reply = ask(f"\n[ASK] {r.reason}.\n      Relax {r.field} from {r.strict_value:g} to {r.relaxed_value:g}? [Y/n] ")
        relax.append(r.model_copy(update={"accepted": reply.strip().lower() not in ("n", "no")}))
    answers = {}
    for q in contract.open_questions:
        reply = ask(f"\n[ASK] {q.question}\n      (enter = '{q.default_assumption}') > ").strip()
        if reply:
            answers[q.question_id] = reply
    return apply_decisions(contract.model_copy(update={"relaxations": relax}), answers)
