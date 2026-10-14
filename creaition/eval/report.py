"""Markdown + JSON regression report."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


# format a metric for the table (percent, seconds, plain number...)
def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "sec":
        return f"{v / 1000:.1f}"
    if kind == "int":
        return f"{v:,.0f}"
    if kind == "f4":
        return f"{v:.4f}"
    if kind == "f3":
        return f"{v:.3f}"
    return f"{v:.2f}"


# B minus A, plus whether that direction is better or worse
def _delta(a: Any, b: Any, kind: str, higher: bool) -> tuple[str, str]:
    if a is None or b is None:
        return "n/a", ""
    d = b - a
    if abs(d) < 1e-9:
        return "0", "="
    if kind == "pct":
        txt = f"{d * 100:+.1f} pp"
    elif kind == "sec":
        txt = f"{d / 1000:+.1f}"
    elif kind == "int":
        txt = f"{d:+,.0f}"
    else:
        txt = f"{d:+.3f}"
    better = (d > 0) == higher
    return txt, "better" if better else "worse"


def write_report(out: Path, a: dict, b: dict, gate: dict, bench: Optional[dict], metrics: list) -> tuple[Path, Path]:
    ma, mb = a["metrics"], b["metrics"]
    mock = a["summary"]["llm_mode"] == "mock"
    # mock latency is simulated from tokens; live latency is measured model-call time
    lat = "modelled" if mock else "wall-clock"
    L: list[str] = []
    L.append("# Evaluation & regression report\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M')} | mode: **{a['summary']['llm_mode']}** | "
             f"{ma['variants']} variants per config ({len(ma['per_brief'])} briefs)\n")
    if mock:
        L.append("> Offline mock models. These numbers exercise the harness end to end and show how a change is "
                 "gated; they are not claims about DeepSeek/OpenAI model quality. Re-run with `--live` for real numbers.\n")
    L.append("| | config | description |\n|---|---|---|")
    L.append(f"| A (baseline) | `{a['config']}` | {a['description']} |")
    L.append(f"| B (candidate) | `{b['config']}` | {b['description']} |\n")

    # verdict first, then the reasons, then the numbers
    L.append(f"## Gate verdict: **{gate['verdict']}**\n")
    if gate["hard_regressions"]:
        L.append("Hard regressions (block the release):")
        L += [f"- {x}" for x in gate["hard_regressions"]]
    if gate["silent_regressions"]:
        L.append("Silent-regression signals (pass rate looks fine, outputs got worse; needs human sign-off):")
        L += [f"- {x}" for x in gate["silent_regressions"]]
    if not gate["hard_regressions"] and not gate["silent_regressions"]:
        L.append("No hard or silent regressions detected.")
    lo, hi = gate["ci_pass_rate_diff"]
    jlo, jhi = gate["ci_judge_mean_diff"]
    L.append(f"\nBootstrap 95% CI of B - A: pass rate [{lo * 100:+.1f}, {hi * 100:+.1f}] pp; "
             f"judge mean [{jlo:+.2f}, {jhi:+.2f}]; judge-score KS = {gate['judge_ks']}.\n")

    L.append("## Metrics summary\n")
    L.append("| metric | A baseline | B candidate | delta | |\n|---|---:|---:|---:|---|")
    for key, label, higher, kind in metrics:
        d, tag = _delta(ma.get(key), mb.get(key), kind, higher)
        label = label.replace("{lat}", lat)
        L.append(f"| {label} | {_fmt(ma.get(key), kind)} | {_fmt(mb.get(key), kind)} | {d} | {tag} |")
    L.append("")

    L.append("## Deterministic checks per brief\n")
    L.append("| brief | A statuses | B statuses |\n|---|---|---|")
    for bid in ma["per_brief"]:
        L.append(f"| {bid} | {ma['per_brief'][bid]} | {mb['per_brief'].get(bid)} |")
    L.append(f"\nConflict detection by code: A {ma['conflict_detection_accuracy']:.0%}, "
             f"B {mb['conflict_detection_accuracy']:.0%}. Conflicting briefs with no hard conflict at all: "
             f"A {ma['conflicts_missed']}, B {mb['conflicts_missed']} (must be 0). Feasible briefs the model alone "
             f"called a hard conflict: A {ma['false_conflicts_model']}, B {mb['false_conflicts_model']} (their "
             f"variants all go to a human). Conflicting-brief variants auto-approved: "
             f"A {ma['conflicting_brief_auto_approved']}, B {mb['conflicting_brief_auto_approved']} (must be 0); "
             f"held for a human or rejected: A {ma['conflicting_brief_held_for_human']:.0%}, "
             f"B {mb['conflicting_brief_held_for_human']:.0%}. Pass rates above exclude the conflicting brief "
             f"(feasible briefs: {', '.join(ma['feasible_briefs'])}).\n")

    L.append("## LLM-as-judge calibration\n")
    if mock:
        L.append("> The mock judge and the ground truth are built from similar signals, so high agreement here only "
                 "shows that the calibration machinery works. It says nothing about a real judge: see "
                 "`sample_outputs/reports_live/` for the live numbers.\n")
    for name, r in (("A", a), ("B", b)):
        m, c = r["metrics"], r["calibration"]
        L.append(f"**{name} ({r['config']})** - agreement with deterministic ground truth: "
                 f"{_fmt(m['judge_gt_agreement'], 'pct')} over {m['judge_gt_n']} variants, Cohen's kappa = "
                 f"{m['judge_gt_kappa']}; confusion {m['judge_confusion']}. Planted-defect sensitivity: "
                 f"{_fmt(c.get('planted_sensitivity'), 'pct')} of {len(c.get('planted_probes', []))} mutated specs "
                 f"scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = "
                 f"{c.get('self_consistency_mad')}, verdict flip rate = {_fmt(c.get('self_consistency_flip_rate'), 'pct')}.\n")
    probes = a["calibration"].get("planted_probes", [])
    if probes:
        L.append("| variant | archetype | GT after mutation | adherence before -> after | detected |\n|---|---|---|---|---|")
        for p in probes:
            L.append(f"| {p['variant_id']} | {p['archetype']} | {p['gt_after_mutation']} | "
                     f"{p['original_adherence']} -> {p['mutated_adherence']} | {'yes' if p['detected'] else 'NO'} |")
        L.append("")

    if bench:
        r = bench["robustness"]
        L.append("## Benchmark: robustness under chaos (baseline config + rate-based faults)\n")
        L.append(f"Faults injected: {r['faults_injected']}\n")
        L.append("| | clean | chaos |\n|---|---:|---:|")
        L.append(f"| completion rate (not FAILED) | 100.0% | {r['completion_rate']:.1%} |")
        L.append(f"| pass rate | {r['clean_gate_pass_rate']:.1%} | {r['gate_pass_rate']:.1%} |")
        L.append(f"| avg model cost / variant (CNY) | {r['clean_avg_model_cost_cny']:.4f} | {r['avg_model_cost_cny']:.4f} |")
        L.append(f"| degraded-evidence rate | - | {r['degraded_rate']:.1%} |")
        L.append(f"| fallback rate | - | {r['fallback_rate']:.1%} |")
        L.append(f"| schema repair rate | - | {r['repair_rate']:.1%} |")
        L.append(f"| calls served by fallback model | - | {r['route_fallback_rate']:.1%} |\n")
        L.append(f"Status mix under chaos: {r['status_counts']}. Breakers at end: {r['breakers']}\n")
        m = bench["memory"]
        L.append("## Benchmark: memory / teach effect\n")
        L.append("Run 1 learns lessons from its own first-draft DFM failures (plus one designer-taught lesson); "
                 "run 2 reads them.\n")
        L.append("| | before lessons | after lessons |\n|---|---:|---:|")
        for k in m["before"]:
            L.append(f"| {k} | {m['before'][k]} | {m['after'][k]} |")
        L.append("\nLessons in memory:")
        L += [f"- `{l['id']}` ({l['source']}, rule {l['rule']}): {l['text']}" for l in m["lessons"]]
        L.append("")

    md = out / "eval_report.md"
    md.write_text("\n".join(L))
    # the JSON report leaves out the full variant states (too big, already in the run folders)
    strip = lambda r: {k: v for k, v in r.items() if k != "states"}  # noqa: E731
    js = out / "eval_report.json"
    js.write_text(json.dumps({"baseline": strip(a), "candidate": strip(b), "gate": gate, "benchmark": bench},
                             indent=2, default=str))
    return md, js
