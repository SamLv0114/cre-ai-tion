"""`python -m creaition demo`: runs the offline showcase and writes the evidence to sample_outputs/.

Each scenario runs in a subprocess so the simulated crash can't take the demo down with it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def run_demo(args: Any) -> None:
    out = Path(args.out).resolve()
    if out.exists():  # rebuild everything except the live-model report, which costs real money to regenerate
        for child in out.iterdir():
            if child.name == "reports_live":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    ws = out / "workspace"
    py = sys.executable
    notes: list[str] = []

    # run one CLI command in a subprocess, save its output to a log file, print the tail
    def sh(cmd: list[str], log: str, stdin: str | None = None, expect: int = 0) -> str:
        full = [py, "-m", "creaition", "--workspace", str(ws), *cmd]
        print(f"\n$ creaition {' '.join(cmd)}", flush=True)
        t0 = time.monotonic()
        p = subprocess.run(full, cwd=ROOT, input=stdin, capture_output=True, text=True)
        text = p.stdout + ("\n" + p.stderr if p.stderr else "")
        # no machine-specific paths; run folders end up in sample_outputs/runs once the scratch workspace is gone
        text = (text.replace(str(out), "sample_outputs").replace(str(ROOT), ".")
                .replace("sample_outputs/workspace/runs", "sample_outputs/runs"))
        record = (f"$ python -m creaition {' '.join(cmd)}\n# exit code {p.returncode}, "
                  f"{time.monotonic() - t0:.1f}s\n\n{text}")
        (out / log).write_text(record)
        tail = [l for l in text.splitlines() if l.strip()][-6:]
        print("\n".join("   " + l[:160] for l in tail))
        if p.returncode != expect:
            print(f"   !! exit code {p.returncode} (expected {expect}); see {log}")
        return record

    # 00: the final output - every evaluation brief, no faults, no crash
    sh(["run", "--variants", "3", "--run-id", "demo-designs"], "00_final_designs_run.log")

    # 01: scripted faults on brief B2, plus span trees for two variants
    sh(["run", "--config", "configs/fault_demo.json", "--brief-ids", "B2", "--variants", "5", "--run-id",
        "demo-faults"], "01_fault_injection_run.log")
    for v in ("B2-v002", "B2-v003"):
        sh(["trace", "demo-faults", "--variant", v], f"01_trace_tree_{v}.txt")
    notes.append("01: see 01_fault_injection_run.log - grep FAULT / RETRY / REPAIR / RECOVERED / CIRCUIT / DEGRADED")

    # 02: kill the process after 30 steps, resume it, compare with a clean run
    sh(["run", "--brief-ids", "B1", "B3", "B4", "--variants", "3", "--run-id", "demo-resume",
        "--crash-after-steps", "30"], "02a_run_killed_after_30_steps.log", expect=137)
    sh(["todos", "demo-resume"], "02b_todos_after_crash.txt")
    sh(["resume", "demo-resume"], "02c_resume.log")
    ref_ws = out / "workspace_reference"
    # same run again without the crash, to compare against
    subprocess.run([py, "-m", "creaition", "--workspace", str(ref_ws), "run", "--brief-ids", "B1", "B3", "B4",
                        "--variants", "3", "--run-id", "demo-resume", "--quiet"], cwd=ROOT, capture_output=True,
                       text=True)
    eq = _compare_runs(ws, ref_ws, "demo-resume")
    (out / "02d_resume_equivalence.txt").write_text(eq)
    shutil.rmtree(ref_ws, ignore_errors=True)
    print("   " + eq.splitlines()[-1])
    notes.append("02: " + eq.splitlines()[-1])

    # 03: approval gate and Ask, using the fault run
    from .state.store import Store

    st = Store(ws / "creaition.db")
    # pick real variant ids by status, since the exact outcome of the fault run can vary
    by_status: dict[str, list[str]] = {}
    for v in st.get_variants("demo-faults"):
        by_status.setdefault(v["status"], []).append(v["variant_id"])
    st.close()
    log = [sh(["pending", "demo-faults"], "_tmp.txt")]
    if by_status.get("pending_approval"):
        log.append(sh(["approve", "demo-faults", by_status["pending_approval"][0], "--reviewer", "lead.designer",
                       "--note", "checked parting line and texture with tooling"], "_tmp.txt"))
    if by_status.get("needs_human"):
        vid = by_status["needs_human"][0]
        log.append(sh(["approve", "demo-faults", vid, "--reviewer", "auto-bot"], "_tmp.txt", expect=1))
        log.append(sh(["ask", "demo-faults", f"Why does {vid} need a human?"], "_tmp.txt"))
        log.append(sh(["approve", "demo-faults", vid, "--reviewer", "lead.designer",
                       "--note", "re-priced against today's resin quote; snapshot was within 1%"], "_tmp.txt"))
    if by_status.get("rejected"):
        log.append(sh(["ask", "demo-faults", f"Why was {by_status['rejected'][0]} rejected?"], "_tmp.txt"))
    log.append(sh(["ask", "demo-resume", "Which variants are the best for B3?"], "_tmp.txt"))
    log.append(sh(["todos", "demo-faults"], "_tmp.txt"))
    (out / "03_hitl_ask_todos.log").write_text("\n\n".join(log))
    (out / "_tmp.txt").unlink(missing_ok=True)

    # 04: interactive Ask on the conflicting brief
    # scripted designer answers: accept two relaxations, refuse the mass one, answer one question
    answers = "y\ny\nn\nManufacturability first; drop the paint, keep IP67\n\n\n\n\n\n"
    sh(["run", "--brief-ids", "B5", "--variants", "2", "--run-id", "demo-ask-B5", "--interactive"],
       "04_interactive_ask_conflicting_brief.log", stdin=answers)
    notes.append("04: B5 conflicting brief - designer accepted the cost/height relaxations and refused the mass one, "
                 "so the brief is reported infeasible and no variants are generated "
                 "(runs/demo-ask-B5/infeasible_B5.md)")

    # 05: teach a lesson and list memory
    t = sh(["teach", "Rugged briefs: keep PC-ABS walls at 2.5 mm and ribs at 1.25 mm (50%).", "--agent",
            "structural_modeler", "--tags", "durability", "--rule", "RIB_RATIO"], "_tmp.txt")
    t += sh(["lessons"], "_tmp.txt")
    (out / "05_teach_memory.log").write_text(t)
    (out / "_tmp.txt").unlink(missing_ok=True)

    # 06: full eval with the benchmark
    sh(["eval", "--out", str(out / "reports"), "--benchmark"], "06_eval_harness.log")
    # keep the report, drop the scratch databases
    shutil.rmtree(out / "reports" / ".eval_workspace", ignore_errors=True)

    # copy the run artifacts reviewers will want, drop the bulky database
    for run in ("demo-designs", "demo-faults", "demo-resume", "demo-ask-B5"):
        src = ws / "runs" / run
        if src.exists():
            # console.log only repeats the trace's events, which the 0x_*.log files already show
            shutil.copytree(src, out / "runs" / run, ignore=shutil.ignore_patterns("console.log"))
    shutil.rmtree(ws, ignore_errors=True)
    for f in out.rglob("*"):  # scrub absolute paths from copied run artifacts as well
        if f.is_file() and f.suffix in (".log", ".json", ".jsonl", ".txt", ".md"):
            t = f.read_text()
            if str(ROOT) in t:
                f.write_text(t.replace(str(out), "sample_outputs").replace(str(ROOT), ".")
                             .replace("sample_outputs/workspace/runs", "sample_outputs/runs"))
    _write_summary(out, notes)
    print(f"\nDemo complete. Evidence in {out}")


# status, BOM, adherence and step count for every variant, side by side
def _compare_runs(ws_a: Path, ws_b: Path, run_id: str) -> str:
    from .state.store import Store

    def snap(ws: Path) -> dict[str, tuple]:
        st = Store(ws / "creaition.db")
        res = {}
        for v in st.get_variants(run_id):
            r = v["result"] or {}
            res[v["variant_id"]] = (v["status"], (r.get("cost") or {}).get("total_cny"),
                                    ((r.get("review") or {}).get("scores") or {}).get("brief_adherence"),
                                    len(r.get("history") or []))
        run = st.get_run(run_id) or {}
        st.close()
        return res, (run.get("summary") or {})

    a, sa = snap(ws_a)
    b, sb = snap(ws_b)
    lines = ["variant    resumed-run (status, BOM, adherence, steps)        uninterrupted-run"]
    same = 0
    for vid in sorted(set(a) | set(b)):
        ok = a.get(vid) == b.get(vid)
        same += ok
        lines.append(f"{vid:<10} {str(a.get(vid)):<48} {str(b.get(vid))}  {'==' if ok else '!='}")
    killed = 30
    lines.append("")
    lines.append(f"steps executed before the kill: {killed} (committed checkpoints)")
    lines.append(f"on resume: finished variants skipped outright, {sa.get('steps_replayed')} checkpointed steps "
                 f"replayed for in-flight variants (no model calls), {sa.get('steps_executed')} steps newly executed; "
                 f"the uninterrupted run executed {sb.get('steps_executed')}")
    lines.append(f"RESULT: {same}/{len(set(a) | set(b))} variants identical to the uninterrupted run; "
                 f"{killed} + {sa.get('steps_executed')} = {killed + (sa.get('steps_executed') or 0)} executed steps "
                 f"vs {sb.get('steps_executed')} without a crash (no committed step was re-run or re-billed)")
    return "\n".join(lines)


# index of the evidence files plus the headline numbers
def _plain(counts: dict[str, int]) -> str:
    """{'pending_approval': 2, 'rejected': 1} -> '2 pending_approval, 1 rejected'."""
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "none"


def _write_summary(out: Path, notes: list[str]) -> None:
    rep = json.loads((out / "reports" / "eval_report.json").read_text())
    a, b, g = rep["baseline"]["metrics"], rep["candidate"]["metrics"], rep["gate"]
    faults = json.loads((out / "runs" / "demo-faults" / "summary.json").read_text())
    designs = json.loads((out / "runs" / "demo-designs" / "summary.json").read_text())
    injected = faults["faults_injected"]
    open_breakers = [k for k, v in faults["breakers"].items() if v == "open"]
    resume_line = next((n for n in notes if n.startswith("02: ")), "02: see 02d_resume_equivalence.txt")[4:]
    L = ["# Sample outputs",
         "",
         "Everything in this folder was produced by `python -m creaition demo`: offline mock models, no API key, and "
         "the same designs on every run with the same Python version (the exact number of injected retries depends "
         "on timing). The one exception is `reports_live/`, which comes from real DeepSeek models and is "
         "kept when the demo is run again. Mock numbers show that the machinery works; they are not claims about real "
         "model quality.",
         "",
         "## 1. Start here: the final output",
         "",
         "Each `report.md` is a run's final output: per brief, the outcome, then a design card for each shortlisted "
         "variant with a to-scale sketch, the 2D concept, the 3D structure, DFM, cost, judge scores and what changed "
         "from the concept.",
         "",
         "| report | what it covers |",
         "|---|---|",
         f"| [`runs/demo-designs/report.md`](runs/demo-designs/report.md) | **all 6 evaluation briefs**, 3 variants each "
         f"(the impossible B5 gets 2 examples after relaxing): {designs['variants']} variants, "
         f"{_plain(designs['status_counts'])}. Console output: `00_final_designs_run.log`. |",
         f"| [`runs/demo-faults/report.md`](runs/demo-faults/report.md) | the rugged brief B2, 5 variants made while faults "
         f"were injected: {_plain(faults['status_counts'])} |",
         "| [`runs/demo-ask-B5/report.md`](runs/demo-ask-B5/report.md) and [`infeasible_B5.md`](runs/demo-ask-B5/infeasible_B5.md) "
         "| the deliberately impossible brief B5: the designer refused one relaxation, so there are no variants, only "
         "an explanation of why it cannot be made and what would fix it |",
         "| [`reports_live/round2_after_fixes/runs/eval-baseline/report.md`](reports_live/round2_after_fixes/runs/eval-baseline/report.md) "
         "| the same kind of report from **real DeepSeek models** (6 briefs, 2 variants each) |",
         "",
         "Status words: `pending_approval` passed every automated check and waits for a person to sign off; "
         "`needs_human` passed with caveats a person must read; `rejected` failed a check (the reason is listed); "
         "`production_ready` was approved.",
         "",
         "## 2. Reliability: one file per scenario",
         "",
         "| file | scenario | what to look for |",
         "|---|---|---|",
         f"| `01_fault_injection_run.log` | brief B2 with scripted faults: {sum(injected.values())} injected "
         f"({', '.join(f'{n} {k}' for k, n in sorted(injected.items(), key=lambda kv: -kv[1]))}) | lines tagged "
         "`[FAULT]` followed by `[RETRY]`, `[REPAIR]`, `[FALLBACK]` and `[RECOVERED]`; `[CIRCUIT]` when the "
         f"breaker for {', '.join(open_breakers) or 'a model'} opens and later variants skip it; `[THROTTLE]` when a 429 "
         "halves the calls in flight. No variant failed. |",
         "| `01_trace_tree_B2-v002.txt`, `01_trace_tree_B2-v003.txt` | the trace of one variant from that run, as a tree "
         "| each step, each model or tool call under it, and the fault and recovery events on each call |",
         "| `02a_run_killed_after_30_steps.log` | run `demo-resume` (3 briefs × 3 variants, saved in `runs/demo-resume/`); "
         "the process is killed (exit 137) after 30 steps "
         "| the `[CRASH]` line |",
         "| `02b_todos_after_crash.txt` | the saved plan right after the crash | unfinished items and variants still marked "
         "`running` |",
         "| `02c_resume.log` | `resume` on the same run | `replayed from checkpoint: 30` (no model calls for those) |",
         f"| `02d_resume_equivalence.txt` | resumed run vs. a run with no crash | {resume_line} |",
         "| `03_hitl_ask_todos.log` | approval and questions | a person approves a variant; a bot is refused on a variant "
         "with caveats; `ask` answers from the run's own records |",
         "| `04_interactive_ask_conflicting_brief.log` | the Ask gate on the impossible brief B5 | three proposed "
         "relaxations; the designer refuses the mass one, so no variants are made |",
         "| `05_teach_memory.log` | teaching | a designer adds a lesson; `lessons` lists memory |",
         "",
         "## 3. Evaluation",
         "",
         "| file | what it is |",
         "|---|---|",
         "| [`reports/eval_report.md`](reports/eval_report.md) (+ `.json`) | baseline vs. candidate (prompt v2) on the 6 "
         "evaluation briefs: gate verdict, metrics, per-brief results, judge calibration, chaos and memory benchmarks |",
         "| `06_eval_harness.log` | the command that made that report |",
         "| [`reports_live/`](reports_live/NOTES.md) | the same eval on real DeepSeek models, before (`round1_before_fixes/`) "
         "and after (`round2_after_fixes/`) the fixes it led to; start with `NOTES.md` |",
         "",
         f"Mock eval in one line: gate **{g['verdict']}** "
         f"({'; '.join(g['hard_regressions'] + g['silent_regressions']) or 'no regressions'}). "
         f"Pass rate {a['gate_pass_rate']:.0%} → {b['gate_pass_rate']:.0%}, judge average {a['judge_mean']:.2f} → "
         f"{b['judge_mean']:.2f}, model cost per variant CNY {a['avg_model_cost_cny']:.3f} → {b['avg_model_cost_cny']:.3f}, "
         f"concept variety {a['diversity']:.2f} → {b['diversity']:.2f}. The judge agrees with the code-based ground "
         f"truth on {a['judge_gt_agreement']:.0%} of baseline variants (κ {a['judge_gt_kappa']}); the live runs show a "
         "real judge does much worse, see `reports_live/`.",
         "",
         "## 4. Inside each run folder",
         "",
         "| file | what it is |",
         "|---|---|",
         "| `report.md`, `sketches/` | the readable final output and its sketches |",
         "| `variants/<id>.json` | everything about one variant: concept, structure, DFM report, cost, judge review, "
         "step history, token use |",
         "| `summary.json` | run totals, per-brief outcome and shortlist, each brief's constraint contract |",
         "| `trace.jsonl` | every span and event of the run, one JSON object per line |",
         "",
         "There is no database in this folder, so pass file paths to the tools. The commands below show one variant "
         "in readable form, print the trace of one variant as a tree, and rebuild a run's `report.md`:",
         "",
         "```bash",
         "python -m creaition show sample_outputs/runs/demo-designs/variants/B3-v001.json",
         "python -m creaition --workspace sample_outputs trace demo-faults --variant B2-v002",
         "python -m creaition report sample_outputs/runs/demo-designs",
         "```",
         ]
    (out / "DEMO_SUMMARY.md").write_text("\n".join(L) + "\n")
