"""Deterministic domain layer: DFM rules, autofix, geometry, cost, constraint resolver."""

import pytest

from creaition.agents import critic, resolver
from creaition.agents.interpreter import conservative_intent
from creaition.domain import catalog as K
from creaition.domain import cost_model, dfm_rules, geometry as G
from creaition.domain.baseline import baseline_structure, floor_cost_cny
from creaition.domain.schemas import Dims2, FaceDraft, Finish, Material, Undercut

DENS = {m: r.density_g_cm3 for m, r in K.MATERIALS_SNAPSHOT.items()}


def check(spec, contract, anchors=("IA1", "IA2")):
    return dfm_rules.evaluate(spec, contract, DENS, anchors)


def test_baseline_passes_every_rule(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    report = check(spec, contract)
    assert report.passed, [f.rule_id for f in report.failures]
    assert report.open_area_ratio >= 0.15 and report.usb_clearance_mm >= 0.5
    assert report.product_mass_g <= 180


def _part(spec, **upd):
    return spec.model_copy(update={"parts": [p.model_copy(update=upd) for p in spec.parts]})


@pytest.mark.parametrize("mutate,rule", [
    (lambda s: _part(s, nominal_wall_mm=1.3, min_wall_mm=1.3, max_wall_mm=1.3), "WALL_RANGE"),
    (lambda s: _part(s, drafts=[FaceDraft(face="outer_sides", draft_deg=0.5)]), "DRAFT_MIN"),
    (lambda s: _part(s, drafts=[FaceDraft(face="outer_sides", draft_deg=1.5, textured=True)]), "DRAFT_MIN"),
    (lambda s: _part(s, ribs=[r.model_copy(update={"thickness_mm": 1.4}) for r in s.parts[0].ribs]), "RIB_RATIO"),
    (lambda s: _part(s, inner_radius_mm=0.5), "INNER_RADIUS"),
    (lambda s: s.model_copy(update={"vent": s.vent.model_copy(update={"slot_count": 3, "slot_w_mm": 1.0,
                                                                      "slot_l_mm": 12.0})}), "VENT_OPEN_AREA"),
    (lambda s: s.model_copy(update={"usb_opening_mm": Dims2(width=10, height=4)}), "USB_CLEARANCE"),
    (lambda s: s.model_copy(update={"display_window_mm": Dims2(width=40, height=18)}), "DISPLAY_WINDOW_MIN"),
    (lambda s: _part(s, undercuts=[Undercut(undercut_id="snap_1", feature="snap hook")]), "UNDERCUT_DECLARED"),
    (lambda s: _part(s, finish=Finish.TEXTURE_HEAVY, drafts=[FaceDraft(face="outer_sides", draft_deg=2.5)]),
     "TEXTURE_DECLARED"),
])
def test_rule_fails_and_autofix_repairs(contract, concept, mutate, rule):
    spec = mutate(baseline_structure(concept, contract, 0, "test"))
    report = check(spec, contract)
    assert rule in {f.rule_id for f in report.failures}
    assert report.only_auto_fixable
    fixed = critic.autofix(spec, report).artifact
    assert check(fixed, contract).passed
    assert fixed.revision == spec.revision + 1


def test_design_level_failures_are_not_auto_fixable(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    slim = spec.model_copy(update={"envelope_mm": spec.envelope_mm.model_copy(update={"height": 23.0})})
    report = check(slim, contract)
    fail = {f.rule_id: f.fix for f in report.failures}
    assert fail.get("PACKAGING_FIT") == "design"
    heavy_contract = contract.model_copy(update={"max_mass_g": 120.0})
    assert "MASS_MAX" in {f.rule_id for f in check(spec, heavy_contract).failures}


def test_intent_trace_must_reference_real_features(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    broken = spec.model_copy(update={"intent_trace": [t.model_copy(update={"realized_by": ["no_such_feature"]})
                                                      for t in spec.intent_trace]})
    assert "INTENT_TRACE_COMPLETE" in {f.rule_id for f in check(broken, contract).failures}


def test_local_fallback_engine_skips_advisory_checks(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    full = dfm_rules.evaluate(spec, contract, DENS, [], full=True)
    local = dfm_rules.evaluate(spec, contract, DENS, [], full=False)
    assert local.confidence == "reduced" and full.confidence == "full"
    assert {r.severity for r in local.results} == {"hard"}


def test_cost_model_amortises_tooling_and_side_actions(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    base = cost_model.estimate(spec, K.MATERIALS_SNAPSHOT, cost_model.tooling_quote_from_rates(spec), 18)
    tooling_line = next(l.cny for l in base.lines if l.item == "tooling_amortised")
    assert tooling_line == pytest.approx(base.tooling_total_cny / 20_000, abs=1e-3)
    with_slider = _part(spec, undercuts=[Undercut(undercut_id="u1", feature="flush window", mechanism="slider",
                                                  side_action_cost_cny=12000)])
    more = cost_model.estimate(with_slider, K.MATERIALS_SNAPSHOT, cost_model.tooling_quote_from_rates(with_slider), 18)
    assert more.total_cny > base.total_cny
    assert 9 < floor_cost_cny([Material.ABS], "none") < 18


def test_geometry_open_area_and_usb_clearance(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    assert G.open_area_ratio(spec) == pytest.approx(7 * 1.2 * 16 / 400, abs=1e-3)
    assert G.usb_clearance(spec) == pytest.approx(min((11 - 8.94) / 2, (5 - 3.26) / 2), abs=1e-3)


def test_resolver_flags_conflicting_brief(briefs):
    b5 = briefs["B5"]
    from creaition.llm.mock import interpret_brief, DEFAULT_PROFILE
    import random
    from creaition.domain.schemas import DesignIntent
    intent = DesignIntent.model_validate(interpret_brief({"brief": b5.model_dump()}, random.Random(1), DEFAULT_PROFILE))
    contract = resolver.resolve(b5, intent)
    assert contract.conflict_detected and contract.requires_human_resolution
    assert {r.field for r in contract.relaxations} == {"cost_ceiling_cny", "max_height_mm", "max_mass_g"}
    # batch default: relaxed only as far as needed (floor + headroom, rounded up), recorded as an assumption
    assert contract.cost_ceiling_cny == 16.0 and contract.max_mass_g == 151.0
    assert contract.max_envelope_mm.height == 26.0
    assert any("Relaxed cost_ceiling_cny" in a for a in contract.assumptions)


def test_resolver_feasible_brief_has_no_relaxations(briefs):
    contract = resolver.resolve(briefs["B1"], conservative_intent(briefs["B1"]))
    assert not contract.relaxations
    assert contract.cost_ceiling_cny == 12.0


def test_resolver_cross_checks_model_numbers(briefs):
    b1 = briefs["B1"]
    wrong = conservative_intent(b1).model_copy(update={"target_unit_cost_cny": 15.0})  # model misread CNY 12
    contract = resolver.resolve(b1, wrong)
    assert contract.cost_ceiling_cny == 12.0  # stricter of model (15) and deterministic text pass (12)
    assert any(q.question_id == "QX-target_unit_cost_cny" for q in contract.open_questions)


def test_refused_relaxation_keeps_strict_limit(briefs):
    from creaition.observability.tracing import NullTracer
    from creaition.llm.mock import interpret_brief, DEFAULT_PROFILE
    import random
    from creaition.domain.schemas import DesignIntent
    b5 = briefs["B5"]
    intent = DesignIntent.model_validate(interpret_brief({"brief": b5.model_dump()}, random.Random(1), DEFAULT_PROFILE))
    contract = resolver.resolve(b5, intent)
    replies = iter(["y", "y", "n", "manufacturability first"] + [""] * 10)
    decided = resolver.clarify(contract, NullTracer(), interactive=True, ask=lambda _q: next(replies))
    assert decided.max_mass_g == 120.0 and decided.cost_ceiling_cny == 16.0
    assert any("answered by designer" in a for a in decided.assumptions)


def test_vent_must_fit_its_face_and_autofix_keeps_proportions(contract, concept):
    spec = baseline_structure(concept, contract, 0, "test")
    # live-run regression: a 60 x 60 mm intake zone placed on a 27 mm-tall top face
    big = spec.model_copy(update={"vent": spec.vent.model_copy(update={
        "face": "top", "zone_w_mm": 60, "zone_h_mm": 60, "slot_count": 26, "slot_w_mm": 1.2, "slot_l_mm": 56})})
    assert "VENT_FITS_FACE" in {f.rule_id for f in check(big, contract).failures}
    fixed = critic.autofix(big, check(big, contract)).artifact
    assert check(fixed, contract).passed
    # a wide, short louver must stay wide and short (the old autofix squared it)
    wide = spec.model_copy(update={"vent": spec.vent.model_copy(update={
        "face": "front", "zone_w_mm": 50, "zone_h_mm": 12, "slot_count": 3, "slot_w_mm": 1.2, "slot_l_mm": 46})})
    fixed = critic.autofix(wide, check(wide, contract)).artifact
    assert fixed.vent.zone_w_mm == 50 and fixed.vent.zone_h_mm == 20 and check(fixed, contract).passed


def test_resolver_ignores_model_restating_platform_limits(briefs):
    b4 = briefs["B4"]  # names no cost target
    restated = conservative_intent(b4).model_copy(update={"target_unit_cost_cny": 18.0, "max_mass_g": 180.0})
    contract = resolver.resolve(b4, restated)
    assert not any(q.question_id.startswith("QX-") for q in contract.open_questions)
    assert contract.cost_ceiling_cny == 18.0


def test_overlong_prose_is_truncated_by_code_not_rejected():
    # live finding: deepseek-flash wrote 120+ char colour names even after two repair prompts
    from creaition.domain.schemas import CMF
    c = CMF(material="ABS", finish="smooth_matte", color="warm sand " * 40)
    assert len(c.color) == 120 and c.color.endswith("…")


def test_platform_limits_are_never_relaxed(briefs, monkeypatch):
    # if even the cheapest / lightest housing breaks a platform limit, nothing may be relaxed past it
    from creaition.domain import baseline as B
    monkeypatch.setattr(B, "floor_cost_cny", lambda *a: 25.0)
    monkeypatch.setattr(B, "floor_mass_g", lambda *a: 200.0)
    contract = resolver.resolve(briefs["B1"], conservative_intent(briefs["B1"]))
    assert contract.cost_ceiling_cny <= 18 and contract.max_mass_g <= 180
    assert not any(r.field in ("cost_ceiling_cny", "max_mass_g") for r in contract.relaxations)
    assert contract.infeasible and len(contract.platform_blockers) == 2
    assert all(c.severity == "hard" for c in contract.conflicts if c.conflict_id.startswith("P"))


def test_refused_relaxation_makes_brief_infeasible(briefs):
    from creaition.observability.tracing import NullTracer
    from creaition.llm.mock import interpret_brief, DEFAULT_PROFILE
    import random
    from creaition.domain.schemas import DesignIntent
    b5 = briefs["B5"]
    intent = DesignIntent.model_validate(interpret_brief({"brief": b5.model_dump()}, random.Random(1), DEFAULT_PROFILE))
    contract = resolver.resolve(b5, intent)
    assert not contract.infeasible  # batch defaults accept the relaxations
    replies = iter(["y", "y", "n"] + [""] * 10)  # designer refuses the mass relaxation
    decided = resolver.clarify(contract, NullTracer(), interactive=True, ask=lambda _q: next(replies))
    assert decided.infeasible and "max_mass_g" in decided.infeasible_reasons[0]


def test_out_of_platform_requests_are_flagged_by_code(briefs):
    from creaition.domain.extract import out_of_platform
    from creaition.domain.schemas import DesignBrief
    text = "Brushed aluminium housing, CNC machined, with 4 mm walls for drop resistance."
    found = out_of_platform(text)
    assert len(found) == 3
    brief = DesignBrief(brief_id="BX", title="metal", text=text)
    contract = resolver.resolve(brief, conservative_intent(brief))
    assert contract.conflict_detected and contract.requires_human_resolution
    assert sum(c.conflict_id.startswith("X") for c in contract.conflicts) == 3
    # and no false alarms on the real briefs
    assert all(out_of_platform(b.text) == [] for b in briefs.values())
    assert out_of_platform("2.5 mm walls in PC-ABS") == []


def test_out_of_platform_ignores_look_alike_and_negated_wording():
    from creaition.domain.extract import out_of_platform
    # each of these used to raise a hard conflict (found in review)
    for text in ["Toughened glass display cover", "glass-fibre reinforced PC-ABS", "Steel grey colour",
                 "Ceramic white colour", "Aluminium-coloured paint", "Wood-grain texture", "Machined-look chamfers",
                 "no metal housing please", "avoid CNC", "10 mm wall-mount bracket"]:
        assert out_of_platform(text) == [], text
    # real requests are still caught, including next to an unrelated negation
    assert out_of_platform("No paint, aluminium unibody") == ["a non-plastic material ('aluminium')"]
    assert out_of_platform("wall thickness 1 mm") == ["1 mm walls (platform range 1.5-3.0 mm)"]


def test_mock_draws_ignore_contract_bookkeeping(contract):
    # adding notes to a contract must not redraw the mock's random numbers (it used to move pass rates by 20 pp)
    import asyncio
    from creaition.llm.base import LLMRequest
    from creaition.llm.mock import MockLLM

    from creaition.agents.concept import directions_for

    def concept(c):
        payload = {"contract": c.model_dump(mode="json"), "variant_id": "T1-v001", "direction": directions_for(c, 1)[0]}
        req = LLMRequest(role="concept_generator", model="deepseek-flash", messages=[{"role": "user", "content": "x"}],
                         payload=payload, variant="T1-v001")
        return asyncio.run(MockLLM(time_scale=0.0).complete(req)).text

    noted = contract.model_copy(update={"assumptions": ["QX-ip default: use IP54"], "infeasible_reasons": []})
    assert concept(noted) == concept(contract)
    assert concept(contract.model_copy(update={"cost_ceiling_cny": 12.0})) != concept(contract)


def test_draft_must_cover_both_sides_and_autofix_adds_the_missing_one(contract, concept):
    from creaition.agents.critic import autofix
    spec = baseline_structure(concept, contract, 0, "test")
    one_face = spec.parts[0].model_copy(update={"drafts": [spec.parts[0].drafts[0]]})  # outer_sides only
    spec = spec.model_copy(update={"parts": [one_face] + list(spec.parts[1:])})
    report = dfm_rules.evaluate(spec, contract, DENS)
    assert "DRAFT_COVERAGE" in {f.rule_id for f in report.failures}
    fixed = autofix(spec, report).artifact
    assert "DRAFT_COVERAGE" not in {f.rule_id for f in dfm_rules.evaluate(fixed, contract, DENS).failures}


def test_snap_fit_closure_must_declare_its_hooks(contract, concept):
    from creaition.domain.schemas import Closure
    spec = baseline_structure(concept, contract, 0, "test").model_copy(update={"closure": Closure.SNAP_FITS,
                                                                               "screw_count": 0})
    assert not any(p.undercuts for p in spec.parts)
    report = dfm_rules.evaluate(spec, contract, DENS)
    fail = next(f for f in report.failures if f.rule_id == "SNAP_FIT_UNDERCUT")
    assert fail.fix == "design"  # a trade-off (add hooks or switch to screws), so it goes to the reviser


def test_dropped_concept_feature_is_caught_by_code_and_held_for_a_human(contract, concept):
    # live round 2: the reviser dropped the TPE overmould that made the bumper, wrote "intent preserved",
    # and the judge believed it. Code compares the two specs instead.
    from creaition.domain.drift import concept_drift
    from creaition.domain.schemas import QualityReview, QualityScores, SecondaryOp
    from creaition.orchestrator.planner import gate
    from creaition.orchestrator.state import VariantState
    from creaition.config import RunConfig
    bumpered = concept.model_copy(update={"cmf": concept.cmf.model_copy(
        update={"secondary_ops": [SecondaryOp.TPE_OVERMOLD]})})
    spec = baseline_structure(concept, contract, 0, "test")  # the safe template has no overmould
    assert concept_drift(bumpered, spec) == ["dropped tpe_overmold"]
    assert concept_drift(concept, spec) == []
    dfm = dfm_rules.evaluate(spec, contract, DENS)
    cost = cost_model.estimate(spec, K.MATERIALS_SNAPSHOT, cost_model.tooling_quote_from_rates(spec), 18)
    review = QualityReview(variant_id=spec.variant_id, verdict="accept", rationale="bumper preserved, looks great",
                           scores=QualityScores(brief_adherence=5, form_coherence=5, cmf_fit=5, usability=5,
                                                intent_preservation=5))
    s = VariantState(run_id="r", variant_id=spec.variant_id, brief_id="T1", direction={}, concept=bumpered,
                     structure=spec, dfm=dfm, cost=cost, review=review, judge_attempted=True)
    decision = gate(s, contract, RunConfig(), "done")
    assert decision.status.value == "needs_human" and "dropped tpe_overmold" in decision.caveats[0]
