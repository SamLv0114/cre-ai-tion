"""Runs the evaluation harness end to end (this is the automated eval test command) plus gate unit tests."""

import asyncio
from argparse import Namespace
from pathlib import Path

import pytest

from creaition.eval.harness import cohen_kappa, ks_statistic, regression_gate, run_eval

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp("reports")
    args = Namespace(baseline=str(ROOT / "configs" / "baseline.json"),
                     candidate=str(ROOT / "configs" / "candidate_prompt_v2.json"), variants=4, briefs=None,
                     out=str(out), benchmark=True, live=False)
    code = asyncio.run(run_eval(args))
    import json
    return code, json.loads((out / "eval_report.json").read_text()), (out / "eval_report.md").read_text()


def test_report_written_with_required_sections(report):
    _, data, md = report
    for section in ("Gate verdict", "Metrics summary", "LLM-as-judge calibration", "robustness under chaos",
                    "memory / teach effect"):
        assert section in md
    for key in ("gate_pass_rate", "avg_model_cost_cny", "avg_latency_ms", "judge_mean"):
        assert key in data["baseline"]["metrics"] and key in data["candidate"]["metrics"]


@pytest.mark.parametrize("cfg", ["baseline", "candidate"])
def test_deterministic_checks_hold(report, cfg):
    m = report[1][cfg]["metrics"]
    assert m["schema_valid_rate"] == 1.0
    # every variant that passed: schema-valid, all hard DFM rules pass, cost under the per-variant ceiling
    assert m["passing_meet_hard_checks"] == 1.0
    assert m["conflict_detection_accuracy"] == 1.0
    assert m["conflicting_brief_auto_approved"] == 0
    assert m["conflicting_brief_held_for_human"] == 1.0
    assert m["status_counts"].get("failed", 0) == 0


def test_judge_agrees_with_deterministic_ground_truth(report):
    m = report[1]["baseline"]["metrics"]
    cal = report[1]["baseline"]["calibration"]
    assert m["judge_gt_n"] >= 10
    assert m["judge_gt_agreement"] >= 0.7
    assert cal["planted_sensitivity"] >= 0.75


def test_gate_flags_mode_collapse_as_silent_regression(report):
    gate = report[1]["gate"]
    assert gate["verdict"] in ("REVIEW", "BLOCK")
    assert any("diversity" in s for s in gate["silent_regressions"])


def test_chaos_run_completes_without_failures(report):
    r = report[1]["benchmark"]["robustness"]
    assert sum(r["faults_injected"].values()) > 10
    assert r["completion_rate"] == 1.0


def _m(**kw):
    base = dict(schema_valid_rate=1.0, conflict_detection_accuracy=1.0, conflicting_brief_auto_approved=0,
                conflicts_missed=0, false_conflicts_code=0, false_conflicts_model=0,
                gate_pass_rate=0.8, judge_mean=4.0, avg_model_cost_cny=0.1, p95_latency_ms=60000, diversity=0.5,
                bom_headroom=0.2, dfm_headroom=0.4, fallback_rate=0.0, repair_rate=0.0, route_fallback_rate=0.0,
                intent_preservation_mean=4.5, judge_scores=[4.0] * 10, feasible_briefs=["B1"],
                per_brief={"B1": {"production_ready": 6}})
    base.update(kw)
    return {"metrics": base, "states": []}


def test_gate_unit_rules():
    assert regression_gate(_m(), _m())["verdict"] == "PASS"
    assert regression_gate(_m(), _m(gate_pass_rate=0.6))["verdict"] == "BLOCK"
    assert regression_gate(_m(), _m(avg_model_cost_cny=0.2))["verdict"] == "BLOCK"
    assert regression_gate(_m(), _m(diversity=0.3))["verdict"] == "REVIEW"
    seg = regression_gate(_m(), _m(per_brief={"B1": {"production_ready": 2, "rejected": 4}}))
    assert seg["verdict"] == "BLOCK" and "segment" in seg["hard_regressions"][0]


def test_gate_conflict_rules():
    # a conflicting brief with no hard conflict at all blocks, whatever the baseline did
    assert regression_gate(_m(conflicts_missed=1), _m(conflicts_missed=1))["verdict"] == "BLOCK"
    # live round 2: both configs had the model call feasible B1 "hard". Same as baseline -> no block, no signal
    assert regression_gate(_m(false_conflicts_model=1), _m(false_conflicts_model=1))["verdict"] == "PASS"
    # the candidate makes more model-only false alarms -> a person has to look
    worse = regression_gate(_m(), _m(false_conflicts_model=1))
    assert worse["verdict"] == "REVIEW" and "hard conflict" in worse["silent_regressions"][0]
    # code-proven false alarms cut a brief's fan-out, so more of them than the baseline blocks
    assert regression_gate(_m(), _m(false_conflicts_code=1))["verdict"] == "BLOCK"


def test_conflict_metrics_count_code_and_model_separately(contract):
    from creaition.domain.schemas import ConflictNote
    from creaition.eval.harness import _brief_metrics
    note = ConflictNote(conflict_id="C1", description="target may be difficult", severity="hard",
                        proposed_resolution="check cost", source="llm")
    feasible_llm = contract.model_copy(update={"brief_id": "B1", "conflicts": [note]})
    conflicting = contract.model_copy(update={"brief_id": "B5", "conflicts": [
        note.model_copy(update={"conflict_id": "R1", "source": "rule"})]})
    meta = {"B1": {"expect": {"conflict": False}}, "B5": {"expect": {"conflict": True}}}
    m = _brief_metrics([], {"B1": feasible_llm, "B5": conflicting}, meta)
    assert m["conflict_detection_accuracy"] == 1.0  # judged on code-proven conflicts
    assert m["false_conflicts_model"] == 1 and m["false_conflicts_code"] == 0 and m["conflicts_missed"] == 0


def test_stats_helpers():
    assert cohen_kappa([(True, True), (False, False)] * 5) == 1.0
    assert cohen_kappa([(True, False), (False, True)] * 5) < 0
    assert ks_statistic([1, 2, 3], [1, 2, 3]) == 0.0
