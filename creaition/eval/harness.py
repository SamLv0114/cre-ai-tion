"""Evaluation harness: deterministic checks, LLM-judge calibration, regression gate, benchmark.

Usage: python -m creaition eval [--baseline cfg] [--candidate cfg] [--variants N] [--benchmark]

Each configuration runs in a fresh workspace (no cache, no memory) so the
comparison isolates the change under test. Mock-mode numbers exercise the
harness end to end; they are not claims about the real models.
"""

from __future__ import annotations

import json
import random
import shutil
import statistics
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from ..domain.flags import Degraded
from ..agents import judge as judge_agent
from ..config import RunConfig, load_config
from ..domain import cost_model
from ..domain import catalog as K
from ..domain.schemas import (
    ConceptSpec, CostReport, DesignBrief, DesignContract, DFMReport, QualityReview, StructuralSpec, VariantStatus,
)
from ..observability.tracing import load_trace
from ..orchestrator.runner import build_runtime, execute_run, percentile
from ..orchestrator.state import VariantState
from ..state.memory import LessonMemory
from ..state.store import Store
from .ground_truth import adherence_gt, plant_defect
from .report import write_report

# statuses that mean 'passed every automated check'
PASSING = {VariantStatus.PENDING_APPROVAL, VariantStatus.PRODUCTION_READY, VariantStatus.NEEDS_HUMAN}


def load_eval_briefs(paths: Optional[list[str]] = None) -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2] / "eval" / "briefs"
    files: list[Path] = []
    for p in [Path(x) for x in (paths or [str(root)])]:
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return [json.loads(f.read_text()) for f in files]


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------
def mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


# agreement corrected for chance: 1 = perfect, 0 = no better than guessing
def cohen_kappa(pairs: list[tuple[bool, bool]]) -> Optional[float]:
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


# largest gap between two score distributions (0 = same shape)
def ks_statistic(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    pts = sorted(set(a) | set(b))
    return round(max(abs(sum(x <= p for x in a) / len(a) - sum(x <= p for x in b) / len(b)) for p in pts), 3)


# resample both groups many times to get a 95% range for the difference
def bootstrap_diff(a: list[float], b: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    """95% CI of mean(b) - mean(a)."""
    if not a or not b:
        return (0.0, 0.0)
    rng = random.Random(seed)
    diffs = sorted(statistics.fmean(rng.choices(b, k=len(b))) - statistics.fmean(rng.choices(a, k=len(a)))
                   for _ in range(n))
    return (round(diffs[int(0.025 * n)], 4), round(diffs[int(0.975 * n)], 4))


# how different two concepts are: mostly categorical choices, a bit of size
def concept_distance(a: VariantState, b: VariantState) -> float:
    ca, cb = a.concept, b.concept
    cats = [(ca.silhouette, cb.silhouette), (ca.cmf.material, cb.cmf.material), (ca.cmf.finish, cb.cmf.finish),
            (ca.cmf.color, cb.cmf.color), (ca.intake.face, cb.intake.face),
            (tuple(sorted(o.value for o in ca.cmf.secondary_ops)), tuple(sorted(o.value for o in cb.cmf.secondary_ops)))]
    cat = sum(x != y for x, y in cats) / len(cats)
    ea, eb = ca.envelope_mm, cb.envelope_mm
    num = (abs(ea.length - eb.length) / 30 + abs(ea.width - eb.width) / 20 + abs(ea.height - eb.height) / 8) / 3
    return 0.7 * cat + 0.3 * min(1.0, num)


# average pairwise distance within each brief, then averaged over briefs
def diversity(states: list[VariantState]) -> float:
    by_brief: dict[str, list[VariantState]] = {}
    for s in states:
        if s.concept is not None:
            by_brief.setdefault(s.brief_id, []).append(s)
    vals = []
    for group in by_brief.values():
        pairs = list(combinations(group, 2))
        if pairs:
            vals.append(statistics.fmean(concept_distance(x, y) for x, y in pairs))
    return round(statistics.fmean(vals), 4) if vals else 0.0


# re-validate every artifact the variant ended up with
def schema_valid(s: VariantState) -> bool:
    try:
        for model, obj in ((ConceptSpec, s.concept), (StructuralSpec, s.structure), (DFMReport, s.dfm),
                           (CostReport, s.cost), (QualityReview, s.review)):
            if obj is not None:
                model.model_validate(obj.model_dump(mode="json"))
        return True
    except ValidationError:
        return False


# ---------------------------------------------------------------------------
# one configuration
# ---------------------------------------------------------------------------
async def run_config(cfg: RunConfig, briefs_raw: list[dict], variants: int, workdir: Path, label: str, *,
                     store: Optional[Store] = None, run_id: Optional[str] = None, learn: bool = False,
                     calibrate: bool = True) -> dict[str, Any]:
    ws = workdir / label
    # fresh workspace per config, so caches and lessons from one run can't help the other
    if store is None:
        shutil.rmtree(ws, ignore_errors=True)
        store = Store(ws / "eval.db")
    run_id = run_id or f"eval-{label}"
    # same settings for both sides: clean auto-approval, no cache
    cfg = cfg.model_copy(update={"variants_per_brief": variants, "auto_approve": "clean", "use_cache": False})
    briefs = [DesignBrief.model_validate(b) for b in briefs_raw]
    summary = await execute_run(cfg, store, ws, run_id, briefs, console=False, learn=learn)
    states = [VariantState.model_validate({**v["result"], "status": v["status"]})
              for v in store.get_variants(run_id) if v["result"]]
    contracts = {bid: DesignContract.model_validate(c) for bid, c in summary["contracts"].items()}
    meta = {b["brief_id"]: b for b in briefs_raw}
    metrics = compute_metrics(states, contracts, meta, summary, ws / "runs" / run_id / "trace.jsonl")
    calib = await calibrate_judge(cfg, store, run_id, states, contracts, meta) if calibrate else {}
    metrics["judge_planted_sensitivity"] = calib.get("planted_sensitivity")
    metrics["judge_self_consistency_mad"] = calib.get("self_consistency_mad")
    return {"label": label, "config": cfg.name, "description": cfg.description, "metrics": metrics,
            "calibration": calib, "summary": {k: v for k, v in summary.items() if k != "contracts"},
            "states": states}


def _rate(items: list, pred) -> float:
    """Share of items where pred(item) is true (0 for an empty list)."""
    return round(sum(1 for x in items if pred(x)) / max(1, len(items)), 4)


def _conflicting_briefs(contracts: dict[str, DesignContract], meta: dict[str, dict]) -> list[str]:
    return [b for b in contracts if meta[b].get("expect", {}).get("conflict")]


def _outcome_metrics(states: list[VariantState], feasible: list[VariantState],
                     passing: list[VariantState]) -> dict[str, Any]:
    """Did variants pass the gates, and how much work did it take?"""
    return {
        "variants": len(states),
        "schema_valid_rate": _rate(states, schema_valid),
        "dfm_pass_rate": _rate(states, lambda s: s.dfm and s.dfm.passed),
        "cost_pass_rate": _rate(states, lambda s: s.cost and s.cost.within_budget),
        # every variant that passed the gates must be schema-valid, pass all hard DFM rules and be under its
        # cost ceiling; anything below 1.0 means the gate let something through
        "passing_meet_hard_checks": _rate(passing, lambda s: schema_valid(s) and s.dfm is not None and s.dfm.passed
                                          and s.cost is not None and s.cost.within_budget) if passing else 1.0,
        # pass rates only count feasible briefs; the conflicting one is judged separately
        "gate_pass_rate": round(len(passing) / max(1, len(feasible)), 4),
        "production_ready_rate": _rate(feasible, lambda s: s.status == VariantStatus.PRODUCTION_READY),
        "first_pass_dfm_rate": _rate(states, lambda s: s.first_dfm_failures == []),
        "avg_llm_revisions": mean([s.llm_revisions for s in states]),
        "avg_autofixes": mean([s.autofixes for s in states]),
        "fallback_rate": _rate(states, lambda s: s.fallback_used or any(d in Degraded.FALLBACKS for d in s.degraded)),
        "degraded_rate": _rate(states, lambda s: bool(s.degraded)),
        "status_counts": dict(Counter(s.status.value for s in states)),
    }


def _cost_metrics(states: list[VariantState], passing: list[VariantState], summary: dict) -> dict[str, Any]:
    """Model spend, latency, and how close the passing specs sit to their limits."""
    lat = [s.usage.latency_ms for s in states if s.status != VariantStatus.SKIPPED]
    return {
        # includes the amortised brief interpretation
        "avg_model_cost_cny": round(summary["model_cost_cny_total"] / max(1, len(states)), 5),
        "avg_tokens": mean([s.usage.total_tokens for s in states]),
        "avg_latency_ms": mean(lat),
        "p95_latency_ms": round(percentile(lat, 0.95), 1),
        "avg_bom_cny": mean([s.cost.total_cny for s in passing if s.cost]),
        "bom_headroom": mean([s.cost.headroom_cny / s.cost.ceiling_cny for s in passing if s.cost]),
        "dfm_headroom": mean([s.dfm.headroom for s in passing if s.dfm]),
        "diversity": diversity(states),
    }


def _call_metrics(trace_path: Path) -> dict[str, Any]:
    """Model-call health from the trace: how often replies needed repair or came from a fallback model."""
    spans = [r for r in load_trace(trace_path) if r.get("type") == "span" and r["name"] == "llm.call"]
    served = [r for r in spans if r["attrs"].get("valid") is True]
    return {
        "repair_rate": _rate(spans, lambda r: r["attrs"].get("valid") is False),
        "route_fallback_rate": _rate(served, lambda r: r["attrs"].get("fallback_depth", 0) > 0),
    }


def _judge_metrics(states: list[VariantState], contracts: dict[str, DesignContract],
                   meta: dict[str, dict]) -> dict[str, Any]:
    """Judge scores, and how often the judge agrees with the rule-based ground truth."""
    judged = [s for s in states if s.review is not None]
    pairs: list[tuple[bool, bool]] = []
    confusion: Counter = Counter()
    for s in judged:
        gt = adherence_gt(meta[s.brief_id].get("archetype", ""), contracts[s.brief_id], s.concept, s.structure, s.cost)
        if gt is None:  # conflicting brief: no ground truth
            continue
        says = s.review.scores.brief_adherence >= 4
        pairs.append((says, gt))
        confusion[f"judge={'adherent' if says else 'not'}|gt={'adherent' if gt else 'not'}"] += 1
    return {
        "judge_mean": mean([s.review.overall for s in judged]),
        "judge_scores": [s.review.overall for s in judged],
        "intent_preservation_mean": mean([s.review.scores.intent_preservation for s in judged]),
        "judge_gt_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
        "judge_gt_kappa": cohen_kappa(pairs),
        "judge_gt_n": len(pairs),
        "judge_confusion": dict(confusion),
    }


def _brief_metrics(states: list[VariantState], contracts: dict[str, DesignContract],
                   meta: dict[str, dict]) -> dict[str, Any]:
    """Per-brief checks: conflicts detected, and the conflicting brief never auto-approved."""
    conflicting = _conflicting_briefs(contracts, meta)
    on_conflicting = [s for s in states if s.brief_id in conflicting]
    expected = {b: bool(meta[b].get("expect", {}).get("conflict")) for b in contracts}
    # what counts is the conflict proven by code (it sets the fan-out and the infeasibility report);
    # a model's own "hard" label is tracked separately, because live models use it for "may be difficult"
    detected_ok = [contracts[b].rule_conflict_detected == expected[b] for b in contracts]
    feasible = [b for b in contracts if not expected[b]]
    return {
        "conflict_detection_accuracy": round(sum(detected_ok) / max(1, len(detected_ok)), 4),
        # a conflicting brief that raised no hard conflict at all, from code or model: its variants could be auto-approved
        "conflicts_missed": sum(1 for b in contracts if expected[b] and not contracts[b].conflict_detected),
        "false_conflicts_code": sum(1 for b in feasible if contracts[b].rule_conflict_detected),
        # feasible briefs the model alone called "hard": every variant of them goes to a human
        "false_conflicts_model": sum(1 for b in feasible
                                     if contracts[b].conflict_detected and not contracts[b].rule_conflict_detected),
        # must stay 0
        "conflicting_brief_auto_approved": sum(1 for s in on_conflicting if s.status == VariantStatus.PRODUCTION_READY),
        "conflicting_brief_held_for_human": _rate(
            on_conflicting, lambda s: s.status in (VariantStatus.NEEDS_HUMAN, VariantStatus.REJECTED)),
        "per_brief": {b: dict(Counter(s.status.value for s in states if s.brief_id == b)) for b in contracts},
        "feasible_briefs": sorted(b for b in contracts if b not in conflicting),
    }


def compute_metrics(states: list[VariantState], contracts: dict[str, DesignContract], meta: dict[str, dict],
                    summary: dict, trace_path: Path) -> dict[str, Any]:
    conflicting = _conflicting_briefs(contracts, meta)
    feasible = [s for s in states if s.brief_id not in conflicting]
    passing = [s for s in feasible if s.status in PASSING]
    return {
        **_outcome_metrics(states, feasible, passing),
        **_cost_metrics(states, passing, summary),
        **_call_metrics(trace_path),
        **_judge_metrics(states, contracts, meta),
        **_brief_metrics(states, contracts, meta),
    }


async def calibrate_judge(cfg: RunConfig, store: Store, run_id: str, states: list[VariantState],
                          contracts: dict[str, DesignContract], meta: dict[str, dict]) -> dict[str, Any]:
    """Two probes beyond agreement: planted-defect sensitivity and run-to-run self-consistency."""
    rt = build_runtime(cfg.model_copy(update={"faults": cfg.faults.model_copy(update={"enabled": False})}), store,
                       run_id + "-calib", None, console=False)
    probes = []
    judged = [s for s in states if s.review is not None and s.cost is not None]
    for s in judged:
        arche = meta[s.brief_id].get("archetype", "")
        contract = contracts[s.brief_id]
        # probe 1: take a good variant, break it on purpose, see if the judge notices
        if adherence_gt(arche, contract, s.concept, s.structure, s.cost) is not True:
            continue
        bad = plant_defect(arche, s.structure)
        if bad is None:
            continue
        quote = cost_model.tooling_quote_from_rates(bad)
        bad_cost = cost_model.estimate(bad, K.MATERIALS_SNAPSHOT, quote, contract.cost_ceiling_cny)
        gt_bad = adherence_gt(arche, contract, s.concept, bad, bad_cost)
        res = await judge_agent.run(rt.ctx, contract, s.concept, bad, bad_cost, None)
        if res.artifact is None:
            continue
        probes.append({"variant_id": s.variant_id, "archetype": arche, "gt_after_mutation": gt_bad,
                       "original_adherence": s.review.scores.brief_adherence,
                       "mutated_adherence": res.artifact.scores.brief_adherence,
                       "detected": res.artifact.scores.brief_adherence <= 3})
        if len(probes) >= 8:
            break
    # probe 2: judge the same variant again at higher temperature and compare
    consistency = []
    for s in judged[:8]:
        res = await judge_agent.run(rt.ctx, contracts[s.brief_id], s.concept, s.structure, s.cost, None, sample=1)
        if res.artifact is not None:
            consistency.append({"variant_id": s.variant_id, "first": s.review.overall, "second": res.artifact.overall,
                                "verdict_flip": (s.review.overall >= cfg.judge_accept_threshold)
                                != (res.artifact.overall >= cfg.judge_accept_threshold)})
    rt.tracer.close()
    return {
        "planted_probes": probes,
        "planted_sensitivity": round(sum(p["detected"] for p in probes) / len(probes), 3) if probes else None,
        "self_consistency": consistency,
        "self_consistency_mad": round(statistics.fmean(abs(c["first"] - c["second"]) for c in consistency), 3)
        if consistency else None,
        "self_consistency_flip_rate": round(sum(c["verdict_flip"] for c in consistency) / len(consistency), 3)
        if consistency else None,
    }


# ---------------------------------------------------------------------------
# regression gate
# ---------------------------------------------------------------------------
METRICS = [  # (key, label, higher_is_better, fmt)
    ("gate_pass_rate", "Pass rate, feasible briefs (all automated gates)", True, "pct"),
    ("production_ready_rate", "Production-ready (clean auto-approval)", True, "pct"),
    ("avg_model_cost_cny", "Avg model cost / variant (CNY)", False, "f4"),
    ("avg_latency_ms", "Avg {lat} latency / variant (s)", False, "sec"),
    ("judge_mean", "Judge score (mean overall, 1-5)", True, "f2"),
    ("first_pass_dfm_rate", "First-draft DFM pass rate", True, "pct"),
    ("avg_llm_revisions", "Avg LLM revisions / variant", False, "f2"),
    ("p95_latency_ms", "p95 {lat} latency (s)", False, "sec"),
    ("avg_tokens", "Avg tokens / variant", False, "int"),
    ("diversity", "Concept diversity (mean pairwise distance)", True, "f3"),
    ("intent_preservation_mean", "Judge intent-preservation (mean)", True, "f2"),
    ("bom_headroom", "BOM headroom vs ceiling (passing)", True, "pct"),
    ("dfm_headroom", "DFM rule headroom (passing)", True, "f3"),
    ("fallback_rate", "Deterministic-fallback rate", False, "pct"),
    ("repair_rate", "Schema-repair rate (invalid replies / calls)", False, "pct"),
    ("route_fallback_rate", "Calls served by a fallback model route", False, "pct"),
    ("schema_valid_rate", "Schema validity", True, "pct"),
    ("passing_meet_hard_checks", "Passing variants: schema-valid, all DFM rules pass, under cost ceiling", True, "pct"),
    ("judge_gt_agreement", "Judge vs deterministic GT agreement", True, "pct"),
    ("conflict_detection_accuracy", "Conflict detection accuracy (code)", True, "pct"),
    ("false_conflicts_model", "Feasible briefs the model alone called a hard conflict", False, "int"),
]


def regression_gate(a: dict, b: dict) -> dict[str, Any]:
    ma, mb = a["metrics"], b["metrics"]
    hard, silent = [], []

    # relative change from A to B
    def rel(k: str) -> float:
        return (mb[k] - ma[k]) / ma[k] if ma[k] else 0.0

    if mb["schema_valid_rate"] < 1.0:
        hard.append(f"schema validity {mb['schema_valid_rate']:.0%} < 100%")
    if mb["conflicts_missed"]:
        hard.append(f"{mb['conflicts_missed']} conflicting brief(s) raised no hard conflict")
    # false alarms block only if the candidate makes them worse than the baseline
    if mb["false_conflicts_code"] > ma["false_conflicts_code"]:
        hard.append(f"code raised a hard conflict on {mb['false_conflicts_code']} feasible brief(s) "
                    f"(baseline {ma['false_conflicts_code']})")
    if mb["conflicting_brief_auto_approved"]:
        hard.append(f"{mb['conflicting_brief_auto_approved']} conflicting-brief variant(s) auto-approved")
    if mb["gate_pass_rate"] < ma["gate_pass_rate"] - 0.05:
        hard.append(f"pass rate dropped {ma['gate_pass_rate']:.0%} -> {mb['gate_pass_rate']:.0%}")
    if mb["judge_mean"] < ma["judge_mean"] - 0.3:
        hard.append(f"judge mean dropped {ma['judge_mean']:.2f} -> {mb['judge_mean']:.2f}")
    if rel("avg_model_cost_cny") > 0.25:
        hard.append(f"model cost +{rel('avg_model_cost_cny'):.0%}")
    if rel("p95_latency_ms") > 0.25:
        hard.append(f"p95 latency +{rel('p95_latency_ms'):.0%}")

    for bid in ma["feasible_briefs"]:  # segment check: an aggregate can hide one brief type collapsing
        pa, pb = ma["per_brief"].get(bid, {}), mb["per_brief"].get(bid, {})
        ok_a = sum(v for k, v in pa.items() if k in {s.value for s in PASSING})
        ok_b = sum(v for k, v in pb.items() if k in {s.value for s in PASSING})
        tot = max(1, sum(pa.values()))
        if ok_a - ok_b >= max(2, round(tot / 3)):
            hard.append(f"brief {bid}: passing variants {ok_a}/{tot} -> {ok_b}/{tot} (segment regression)")

    # silent regressions: pass rate holds but something else got worse
    stable = mb["gate_pass_rate"] >= ma["gate_pass_rate"] - 0.02
    if rel("diversity") < -0.15:
        silent.append(f"concept diversity {rel('diversity'):+.0%} (mode collapse) while pass rate "
                      f"{'held' if stable else 'moved'}")
    if mb["judge_mean"] < ma["judge_mean"] - 0.1 and stable:
        silent.append(f"judge mean {ma['judge_mean']:.2f} -> {mb['judge_mean']:.2f} with pass rate stable")
    if ma["bom_headroom"] and rel("bom_headroom") < -0.2:
        silent.append(f"BOM headroom {rel('bom_headroom'):+.0%} (closer to the cost ceiling)")
    if ma["dfm_headroom"] and rel("dfm_headroom") < -0.2:
        silent.append(f"DFM headroom {rel('dfm_headroom'):+.0%} (specs hugging the rule limits)")
    if mb["fallback_rate"] > ma["fallback_rate"] + 0.05:
        silent.append(f"fallback rate {ma['fallback_rate']:.0%} -> {mb['fallback_rate']:.0%}")
    if mb["route_fallback_rate"] > ma["route_fallback_rate"] + 0.05:
        silent.append(f"calls served by fallback models {ma['route_fallback_rate']:.0%} -> {mb['route_fallback_rate']:.0%}")
    if mb["repair_rate"] > ma["repair_rate"] + 0.05:
        silent.append(f"schema repair rate {ma['repair_rate']:.0%} -> {mb['repair_rate']:.0%}")
    if mb["intent_preservation_mean"] < ma["intent_preservation_mean"] - 0.2:
        silent.append(f"intent preservation {ma['intent_preservation_mean']:.2f} -> {mb['intent_preservation_mean']:.2f}")
    if mb["false_conflicts_model"] > ma["false_conflicts_model"]:
        silent.append(f"model called {mb['false_conflicts_model']} feasible brief(s) a hard conflict "
                      f"(baseline {ma['false_conflicts_model']}); all their variants go to a human")
    ks = ks_statistic(ma["judge_scores"], mb["judge_scores"])
    if ks > 0.3:
        silent.append(f"judge score distribution shifted (KS = {ks})")

    # BLOCK on hard regressions, REVIEW (needs a person) on silent ones
    verdict = "BLOCK" if hard else ("REVIEW" if silent else "PASS")
    sa = [1.0 if s.status in PASSING else 0.0 for s in a["states"] if s.brief_id in ma["feasible_briefs"]]
    sb = [1.0 if s.status in PASSING else 0.0 for s in b["states"] if s.brief_id in mb["feasible_briefs"]]
    return {
        "verdict": verdict, "hard_regressions": hard, "silent_regressions": silent, "judge_ks": ks,
        "ci_pass_rate_diff": bootstrap_diff(sa, sb),
        "ci_judge_mean_diff": bootstrap_diff(ma["judge_scores"], mb["judge_scores"]),
    }


# ---------------------------------------------------------------------------
# benchmark extras
# ---------------------------------------------------------------------------
async def memory_effect(base: RunConfig, briefs_raw: list[dict], variants: int, workdir: Path) -> dict[str, Any]:
    """Run once with learning on, then again reading the learned + taught lessons."""
    ws = workdir / "memory"
    shutil.rmtree(ws, ignore_errors=True)
    store = Store(ws / "eval.db")
    # one designer lesson, then run 1 learns more from its own mistakes; run 2 uses them all
    LessonMemory(store).teach("Size ribs at 50% of the nominal wall (40-60% allowed); designers saw sink marks.",
                              agent="structural_modeler", rule_id="RIB_RATIO")
    cfg = base.model_copy(update={"use_memory": False})
    before = await run_config(cfg, briefs_raw, variants, workdir, "memory", store=store, run_id="mem-before",
                              learn=True, calibrate=False)
    cfg_after = base.model_copy(update={"use_memory": True})
    after = await run_config(cfg_after, briefs_raw, variants, workdir, "memory", store=store, run_id="mem-after",
                             calibrate=False)
    lessons = store.list_lessons()
    return {
        "lessons": [{"id": l["lesson_id"], "source": l["source"], "rule": l["rule_id"], "text": l["text"]} for l in lessons],
        "before": {k: before["metrics"][k] for k in ("first_pass_dfm_rate", "avg_llm_revisions", "avg_autofixes",
                                                     "avg_model_cost_cny", "gate_pass_rate")},
        "after": {k: after["metrics"][k] for k in ("first_pass_dfm_rate", "avg_llm_revisions", "avg_autofixes",
                                                   "avg_model_cost_cny", "gate_pass_rate")},
    }


async def robustness(base: RunConfig, chaos_path: Path, briefs_raw: list[dict], variants: int, workdir: Path,
                     clean: dict) -> dict[str, Any]:
    # same briefs with random faults switched on
    chaos = load_config(chaos_path)
    res = await run_config(chaos, briefs_raw, variants, workdir, "chaos", calibrate=False)
    m = res["metrics"]
    return {
        "faults_injected": res["summary"]["faults_injected"],
        "completion_rate": round(1 - m["status_counts"].get("failed", 0) / max(1, m["variants"]), 4),
        "gate_pass_rate": m["gate_pass_rate"], "clean_gate_pass_rate": clean["metrics"]["gate_pass_rate"],
        "fallback_rate": m["fallback_rate"], "degraded_rate": m["degraded_rate"], "repair_rate": m["repair_rate"],
        "route_fallback_rate": m["route_fallback_rate"],
        "avg_model_cost_cny": m["avg_model_cost_cny"], "clean_avg_model_cost_cny": clean["metrics"]["avg_model_cost_cny"],
        "status_counts": m["status_counts"], "breakers": res["summary"]["breakers"],
    }


async def run_eval(args: Any) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".eval_workspace"
    briefs_raw = load_eval_briefs(args.briefs)
    base_cfg, cand_cfg = load_config(args.baseline), load_config(args.candidate)
    if getattr(args, "provider", None):
        base_cfg, cand_cfg = base_cfg.with_provider(args.provider), cand_cfg.with_provider(args.provider)
    if getattr(args, "live", False):
        base_cfg, cand_cfg = base_cfg.for_live(), cand_cfg.for_live()
    # memory off for both, so only the change under test differs
    base_cfg = base_cfg.model_copy(update={"use_memory": False})
    cand_cfg = cand_cfg.model_copy(update={"use_memory": False})
    print(f"[eval] {len(briefs_raw)} briefs x {args.variants} variants | baseline='{base_cfg.name}' "
          f"candidate='{cand_cfg.name}' | mode={base_cfg.llm_mode}")
    a = await run_config(base_cfg, briefs_raw, args.variants, work, "baseline")
    print(f"[eval] baseline done: pass {a['metrics']['gate_pass_rate']:.0%}, judge {a['metrics']['judge_mean']:.2f}")
    b = await run_config(cand_cfg, briefs_raw, args.variants, work, "candidate")
    print(f"[eval] candidate done: pass {b['metrics']['gate_pass_rate']:.0%}, judge {b['metrics']['judge_mean']:.2f}")
    gate = regression_gate(a, b)
    bench = None
    if getattr(args, "benchmark", False):
        chaos_path = Path(__file__).resolve().parents[2] / "configs" / "chaos.json"
        bench = {"robustness": await robustness(base_cfg, chaos_path, briefs_raw, args.variants, work, a),
                 "memory": await memory_effect(base_cfg, briefs_raw, args.variants, work)}
        print("[eval] benchmark done")
    md, js = write_report(out, a, b, gate, bench, METRICS)
    print(f"[eval] verdict: {gate['verdict']}  report: {md}  json: {js}")
    for line in gate["hard_regressions"] + gate["silent_regressions"]:
        print(f"[eval]   - {line}")
    # exit code for CI (used with --strict)
    return {"PASS": 0, "REVIEW": 1, "BLOCK": 2}[gate["verdict"]]
