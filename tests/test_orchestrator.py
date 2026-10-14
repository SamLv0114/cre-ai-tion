"""Planner guards, reducer, idempotent checkpoints, crash/resume equivalence, HITL, memory, cache, ask."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from creaition.config import RunConfig
from creaition.domain.baseline import baseline_structure
from creaition.domain.schemas import DesignBrief, VariantStatus
from creaition.orchestrator import hitl
from creaition.orchestrator.ask import retrieve_facts
from creaition.orchestrator.planner import next_step
from creaition.orchestrator.runner import execute_run
from creaition.orchestrator.state import Step, StepOutcome, VariantState, apply
from creaition.state.memory import LessonMemory, SemanticCache
from creaition.state.store import Store

ROOT = Path(__file__).resolve().parent.parent


# a variant whose structure is too thin to fit the battery (a design-level DFM failure)
def failing_state(contract, concept, **upd) -> VariantState:
    from creaition.domain import catalog as K
    from creaition.domain import dfm_rules
    spec = baseline_structure(concept, contract, 0, "t")
    slim = spec.model_copy(update={"envelope_mm": spec.envelope_mm.model_copy(update={"height": 23.0})})
    dfm = dfm_rules.evaluate(slim, contract, {m: r.density_g_cm3 for m, r in K.MATERIALS_SNAPSHOT.items()}, [])
    base = dict(run_id="r", variant_id="T1-v001", brief_id="T1", direction={"name": "d"}, concept=concept,
                structure=slim, structure_source="reviser", dfm=dfm, steps=5)
    base.update(upd)
    return VariantState(**base)


def test_planner_escalates_design_failures_to_reviser(contract, concept):
    d = next_step(failing_state(contract, concept), RunConfig())
    assert d.step == Step.REVISE


@pytest.mark.parametrize("upd,why", [
    ({"llm_revisions": 3}, "revision cap"),
    ({"oscillation": True}, "oscillation"),
    ({"budget_exhausted": True}, "budget"),
    ({"reviser_failures": 2}, "reviser failed"),
])
def test_loop_guards_route_to_deterministic_fallback(contract, concept, upd, why):
    d = next_step(failing_state(contract, concept, **upd), RunConfig())
    assert d.step == Step.FALLBACK_BASELINE and why in d.reason


def test_no_way_back_into_the_loop_after_fallback(contract, concept):
    d = next_step(failing_state(contract, concept, fallback_used=True), RunConfig())
    assert d.step == Step.FINALIZE


def test_step_guard_finalizes(contract, concept):
    d = next_step(failing_state(contract, concept, steps=18), RunConfig())
    assert d.step == Step.FINALIZE and "step guard" in d.reason


def test_reducer_detects_oscillation(contract, concept):
    spec = baseline_structure(concept, contract, 1, "t")
    s = VariantState(run_id="r", variant_id="v", brief_id="T1", direction={}, concept=concept)
    out = StepOutcome(step=Step.REVISE, artifact_type="StructuralSpec", artifact=spec.model_dump(mode="json"))
    s1 = apply(s, Step.REVISE, out)
    assert not s1.oscillation
    again = spec.model_copy(update={"revision": 2, "change_log": ["different log, same geometry"]})
    s2 = apply(s1, Step.REVISE, StepOutcome(step=Step.REVISE, artifact_type="StructuralSpec",
                                            artifact=again.model_dump(mode="json")))
    assert s2.oscillation and s2.llm_revisions == 2


def _briefs(*ids):
    out = []
    for f in sorted((ROOT / "eval" / "briefs").glob("*.json")):
        b = DesignBrief.model_validate(json.loads(f.read_text()))
        if b.brief_id in ids:
            out.append(b)
    return out


def test_rerun_is_idempotent_and_free(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = RunConfig(variants_per_brief=3)
    first = asyncio.run(execute_run(cfg, store, tmp_path, "r1", _briefs("B1", "B4"), console=False))
    second = asyncio.run(execute_run(cfg, store, tmp_path, "r1", _briefs("B1", "B4"), console=False))
    assert first["steps_executed"] > 0 and first["steps_replayed"] == 0
    assert second["steps_executed"] == 0  # every step served from checkpoints: nothing re-billed
    assert second["status_counts"] == first["status_counts"]
    assert store.count_steps("r1") == first["steps_executed"]  # INSERT OR IGNORE: no duplicate artifacts


def test_every_variant_terminal_and_schema_valid(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(RunConfig(variants_per_brief=2), store, tmp_path, "r2", _briefs("B2", "B5"),
                                console=False))
    for v in store.get_variants("r2"):
        st = VariantState.model_validate(v["result"])
        assert st.status.terminal and st.status != VariantStatus.FAILED
    assert s["briefs"]["B5"]["conflict_detected"]
    assert all(v["status"] in ("needs_human", "rejected") for v in store.get_variants("r2") if v["brief_id"] == "B5")


# run the real CLI in a subprocess (needed so os._exit can kill it)
def _cli(ws, *args, check=True):
    p = subprocess.run([sys.executable, "-m", "creaition", "--workspace", str(ws), *args], cwd=ROOT,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(p.stderr[-2000:])
    return p


def test_kill_and_resume_matches_uninterrupted_run(tmp_path):
    args = ["run", "--brief-ids", "B1", "B3", "--variants", "3", "--run-id", "rk", "--quiet"]
    killed = _cli(tmp_path / "a", *args, "--crash-after-steps", "20", check=False)
    assert killed.returncode == 137
    _cli(tmp_path / "a", "resume", "rk", "--quiet")
    _cli(tmp_path / "b", *args)

    def snap(ws):
        st = Store(ws / "creaition.db")
        run = st.get_run("rk")
        res = {v["variant_id"]: (v["status"], (v["result"].get("cost") or {}).get("total_cny"),
                                 v["result"]["history"]) for v in st.get_variants("rk")}
        return res, run["summary"]

    a, sa = snap(tmp_path / "a")
    b, sb = snap(tmp_path / "b")
    assert a == b  # same statuses, BOMs and step histories as a run that was never killed
    # finished variants are skipped outright; in-flight ones replay their committed steps
    assert 0 < sa["steps_replayed"] <= 20
    assert 20 + sa["steps_executed"] == sb["steps_executed"]  # no committed step executed (or billed) twice


def test_hitl_policy_never_auto_approves_caveated_variants(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    asyncio.run(execute_run(RunConfig(variants_per_brief=2, auto_approve="clean"), store, tmp_path, "r3",
                            _briefs("B4", "B5"), console=False))
    statuses = {v["variant_id"]: v["status"] for v in store.get_variants("r3")}
    assert all(s != "production_ready" for vid, s in statuses.items() if vid.startswith("B5"))
    approvals = store.get_approvals("r3")
    assert all(a["reviewer"] == "auto-policy:clean" for a in approvals.values())
    caveated = [vid for vid, s in statuses.items() if s == "needs_human"]
    if caveated:
        assert hitl.decide(store, "r3", caveated[0], approve=True, reviewer="auto-bot") is not None
        assert hitl.decide(store, "r3", caveated[0], approve=True, reviewer="lead") is None
        assert store.get_variant("r3", caveated[0])["status"] == "production_ready"


def test_lessons_are_learned_retrieved_and_snapshotted(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    mem = LessonMemory(store)
    learned = mem.learn_from_first_drafts([["RIB_RATIO"], ["RIB_RATIO", "DRAFT_MIN"], ["RIB_RATIO"], []])
    assert [l.rule_id for l in learned] == ["RIB_RATIO"]
    assert mem.learn_from_first_drafts([["RIB_RATIO"]] * 4) == []  # deduplicated
    mem.teach("Use 2.5 mm walls outdoors", agent="structural_modeler", tags=["outdoor"])
    snap = mem.snapshot()
    got = snap.for_agent("structural_modeler", {"outdoor"})
    assert {l.source for l in got} == {"human", "auto"}
    assert snap.for_agent("concept_generator", {"indoor"}) == []
    assert mem.snapshot(enabled=False).lessons == ()


def test_semantic_cache_requires_matching_numeric_guard(tmp_path):
    from creaition.domain.extract import extract
    cache = SemanticCache(Store(tmp_path / "db.sqlite"))
    a = "Cheap retail detector, target CNY 12 per housing, plain look, printed logo only."
    cache.put("intent", a, extract(a).guard(), {"x": 1})
    near = "Cheap retail detector, target CNY 12 per housing, plain look, printed logo only!"
    other_price = "Cheap retail detector, target CNY 15 per housing, plain look, printed logo only."
    assert cache.lookup("intent", near, extract(near).guard())[0] == {"x": 1}
    hit, sim = cache.lookup("intent", other_price, extract(other_price).guard())
    assert hit is None


def test_ask_retrieves_grounded_facts(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    asyncio.run(execute_run(RunConfig(variants_per_brief=2), store, tmp_path, "r4", _briefs("B5"), console=False))
    facts = retrieve_facts(store, "r4", "Why was B5-v001 rejected?")
    assert facts and all("B5-v001" in f for f in facts)


def test_mixed_preset_runs_offline_and_routes_with_params(tmp_path):
    cfg = RunConfig(variants_per_brief=2).with_provider("mixed")
    assert cfg.routes["quality_judge"][0].provider == "openai" and cfg.routes["quality_judge"][1].provider == "deepseek"
    assert cfg.routes["structural_modeler"][0].params == {"thinking": {"type": "disabled"}}
    assert cfg.routes["concept_generator"][0].model != cfg.routes["quality_judge"][0].model
    assert not cfg.validate_routing()
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(cfg, store, tmp_path, "rd", _briefs("B1"), console=False))
    assert s["status_counts"] and "failed" not in s["status_counts"]


def test_reviser_recovers_from_invalid_tool_args(tmp_path, contract, concept):
    """Live regression: a real model sent {"drop_secondary_ops": true}. The step must not crash; the error goes back
    to the model as a tool result and the model can still finish its revision."""
    import json as _json
    from creaition.agents import reviser
    from creaition.domain import catalog as K, cost_model, dfm_rules
    from creaition.llm.base import LLMResponse
    from creaition.orchestrator.runner import build_runtime
    from creaition.reliability.budget import Budget

    spec = baseline_structure(concept, contract, 0, "t")
    replies = iter([
        {"step": {"action": "tool_call", "tool": "cost_what_if", "args": {"material": "ABS", "drop_secondary_ops": True}}},
        {"step": {"action": "final", "structure": spec.model_dump(mode="json"), "changes": ["kept design"]}},
    ])
    seen = []

    # fake model that replies with a fixed sequence of answers
    class Scripted:
        provider = "scripted"

        async def complete(self, req):
            seen.append(req.messages[-1]["content"])
            return LLMResponse(text=_json.dumps(next(replies)), model=req.model, provider="scripted",
                               prompt_tokens=10, completion_tokens=10, latency_ms=1.0)

    rt = build_runtime(RunConfig(), Store(tmp_path / "db.sqlite"), "r", None, console=False)
    rt.router.clients = {p: Scripted() for p in ("deepseek", "deepseek_anthropic", "openai")}
    dens = {m: r.density_g_cm3 for m, r in K.MATERIALS_SNAPSHOT.items()}
    dfm = dfm_rules.evaluate(spec, contract, dens, [])
    cost = cost_model.estimate(spec, K.MATERIALS_SNAPSHOT, cost_model.tooling_quote_from_rates(spec), 18)
    res = asyncio.run(reviser.run(rt.ctx, contract, concept, spec, dfm, cost, Budget(80_000, 0.6)))
    assert res.artifact is not None and res.artifact.revision == spec.revision + 1
    assert "invalid tool arguments" in seen[-1]  # the model was told what was wrong


def test_infeasible_brief_is_not_fanned_out(tmp_path, monkeypatch):
    from creaition.domain import baseline as B
    monkeypatch.setattr(B, "floor_cost_cny", lambda *a: 25.0)  # above the CNY 18 platform ceiling
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(RunConfig(variants_per_brief=3), store, tmp_path, "rinf", _briefs("B1"),
                                console=False))
    assert s["briefs"]["B1"]["infeasible"] and s["variants"] == 0
    assert (tmp_path / "runs" / "rinf" / "infeasible_B1.md").exists()
    assert store.get_variants("rinf") == []


def test_relaxed_brief_gets_a_few_example_variants(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(RunConfig(variants_per_brief=5), store, tmp_path, "rrel", _briefs("B1", "B5"),
                                console=False))
    per_brief = {b: sum(v.values()) for b, v in ((b, s["briefs"][b]["statuses"]) for b in ("B1", "B5"))}
    assert per_brief == {"B1": 5, "B5": 2}


def _ranked(vid, look, judge):
    from types import SimpleNamespace as NS
    return NS(variant_id=vid, status=VariantStatus.PENDING_APPROVAL, review=NS(overall=judge),
              cost=NS(headroom_cny=1.0, ceiling_cny=18.0), dfm=NS(headroom=0.5),
              concept=NS(silhouette=look, cmf=NS(material="ABS")))


def test_shortlist_prefers_new_looks_but_always_fills_k():
    from creaition.orchestrator.runner import shortlist
    # 4 of 5 share a look: diversity first, then fill the free slots by score (used to return only 2)
    states = [_ranked("v1", "pebble", 5), _ranked("v2", "pebble", 4.8), _ranked("v3", "pebble", 4.6),
              _ranked("v4", "pebble", 4.4), _ranked("v5", "slab", 3.0)]
    assert shortlist(states, 3) == ["v1", "v5", "v2"]
    same = [_ranked(f"v{i}", "pebble", 5 - i / 10) for i in range(1, 6)]
    assert shortlist(same, 3) == ["v1", "v2", "v3"]  # used to return only 1


def test_only_rule_conflicts_cut_the_fan_out(contract):
    from creaition.domain.schemas import ConflictNote
    from creaition.orchestrator.runner import planned_variants
    cfg = RunConfig(variants_per_brief=5)
    llm_hard = ConflictNote(conflict_id="C1", description="model thinks matte and glossy clash", severity="hard",
                            proposed_resolution="pick one", source="llm")
    llm_only = contract.model_copy(update={"conflicts": [llm_hard]})
    assert planned_variants(llm_only, cfg) == 5  # still goes to a human, but gets the full fan-out
    rule = llm_hard.model_copy(update={"conflict_id": "X1", "source": "rule"})
    assert planned_variants(contract.model_copy(update={"conflicts": [rule]}), cfg) == 2


def _outcome(brief, i, status, reason="", first_fail=("RIB_RATIO",)):
    return VariantState(run_id="r", variant_id=f"{brief}-v{i:03d}", brief_id=brief, direction={}, status=status,
                        reason=reason, first_dfm_failures=list(first_fail))


def test_quality_breaker_pauses_only_the_failing_brief():
    from creaition.observability.tracing import NullTracer
    from creaition.orchestrator.quality import BriefQuality, wilson_lower
    assert wilson_lower(10, 10) > 0.7 > wilson_lower(9, 10)  # 9/10 is not yet sure enough
    q = BriefQuality(RunConfig(), NullTracer())
    for i in range(12):
        q.record(_outcome("B1", i, VariantStatus.REJECTED, "off-brief: judge brief_adherence 3 < 4"))
        q.record(_outcome("B2", i, VariantStatus.REJECTED if i % 3 == 0 else VariantStatus.PENDING_APPROVAL,
                          "quality 3.1 below bar"))
    assert "same cause: off-brief (judge)" in q.paused["B1"]
    assert "B2" not in q.paused  # a third rejected is a hard brief, not a broken strategy


def test_first_draft_failures_do_not_trip_the_quality_breaker():
    from creaition.observability.tracing import NullTracer
    from creaition.orchestrator.quality import BriefQuality
    q = BriefQuality(RunConfig(), NullTracer())
    for i in range(40):  # every first draft failed DFM, the loop fixed them all: that's the design working
        q.record(_outcome("B1", i, VariantStatus.PENDING_APPROVAL, first_fail=("DRAFT_MIN", "VENT_OPEN_AREA")))
    assert q.paused == {}


def test_paused_brief_skips_its_remaining_variants(tmp_path):
    # an impossible quality bar rejects every variant of B1 for the same reason; after 10 the brief is paused
    cfg = RunConfig(variants_per_brief=20, max_concurrency=1, judge_accept_threshold=5.1)
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(cfg, store, tmp_path, "rq", _briefs("B1"), console=False))
    b1 = s["briefs"]["B1"]
    assert b1["statuses"] == {"rejected": 10, "skipped": 10}
    assert "same cause: low quality (judge)" in b1["quality_paused"]
    skipped = [v for v in store.get_variants("rq") if v["status"] == "skipped"]
    assert all(v["result"]["reason"].startswith("quality breaker:") for v in skipped)
    # a designer reviewed it and resumes with the breaker off: the skipped variants run, nothing is re-billed
    s2 = asyncio.run(execute_run(cfg.model_copy(update={"quality_breaker": False}), store, tmp_path, "rq",
                                 _briefs("B1"), console=False))
    assert s2["briefs"]["B1"]["statuses"] == {"rejected": 20} and s2["steps_replayed"] > 0


def test_show_renders_a_variant_and_lists_a_run(tmp_path):
    from creaition.show import list_variants, render_variant
    store = Store(tmp_path / "db.sqlite")
    asyncio.run(execute_run(RunConfig(variants_per_brief=2), store, tmp_path, "rs", _briefs("B3"), console=False))
    variants = store.get_variants("rs")
    assert list_variants(variants).count("\n") == 1  # one line per variant
    text = render_variant(variants[0])
    for section in ("-- 2D concept", "-- 3D structure", "changed from the concept (code check)", "-- DFM", "-- Cost"):
        assert section in text
    saved = json.loads((tmp_path / "runs" / "rs" / "variants" / f"{variants[0]['variant_id']}.json").read_text())
    assert render_variant(saved) == text  # the saved file and the store give the same view


def test_every_run_writes_a_readable_design_report(tmp_path):
    import xml.etree.ElementTree as ET
    from creaition.design_report import build_from_folder
    store = Store(tmp_path / "db.sqlite")
    s = asyncio.run(execute_run(RunConfig(variants_per_brief=2), store, tmp_path, "rr", _briefs("B2", "B5"),
                                console=False))
    run_dir = tmp_path / "runs" / "rr"
    report = (run_dir / "report.md").read_text()
    assert "## B2" in report and "## B5" in report
    for vid in s["briefs"]["B2"]["shortlist"]:
        assert f"sketches/{vid}.svg" in report
        ET.parse(run_dir / "sketches" / f"{vid}.svg")  # valid SVG / XML
    # the same report can be rebuilt from the saved folder alone (how the live runs get theirs)
    (run_dir / "report.md").unlink()
    assert build_from_folder(run_dir).read_text().startswith("# Design report: rr")
