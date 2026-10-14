"""Command-line entry point: `python -m creaition <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .config import RunConfig, load_config
from .domain.schemas import DesignBrief
from .observability.tracing import load_trace, render_tree
from .state.memory import LessonMemory
from .state.store import Store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRIEFS = ROOT / "eval" / "briefs"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from a git-ignored .env (never overrides the real environment, never printed)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# brief files or folders -> DesignBrief list (the eval-only fields are dropped here)
def load_briefs(paths: Optional[list[str]] = None, ids: Optional[list[str]] = None) -> list[DesignBrief]:
    files: list[Path] = []
    for p in [Path(x) for x in (paths or [str(DEFAULT_BRIEFS)])]:
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    briefs = [DesignBrief.model_validate(json.loads(f.read_text())) for f in files]
    if ids:
        briefs = [b for b in briefs if b.brief_id in ids]
    if not briefs:
        raise SystemExit("no briefs selected")
    return briefs


def store_for(workspace: str) -> Store:
    return Store(Path(workspace) / "creaition.db")


# short human-readable run summary for the terminal
def print_summary(s: dict) -> None:
    lat = "modelled" if s["llm_mode"] == "mock" else "wall-clock"  # mock latency is simulated from tokens
    print("\n=== run summary:", s["run_id"], f"(config={s['config']}, mode={s['llm_mode']}) ===")
    print(f"variants: {s['variants']}  status: {s['status_counts']}")
    print(f"first-pass DFM: {s['first_pass_dfm_rate']:.0%}  avg LLM revisions: {s['avg_llm_revisions']}  "
          f"avg autofixes: {s['avg_autofixes']}  fallback rate: {s['fallback_rate']:.0%}")
    print(f"tokens: {s['tokens_total']}  model cost: CNY {s['model_cost_cny_total']} "
          f"(CNY {s['model_cost_cny_per_variant']}/variant)  {lat} latency p50/p95: "
          f"{s['modeled_latency_ms_p50'] / 1000:.1f}s / {s['modeled_latency_ms_p95'] / 1000:.1f}s  wall: {s['wall_clock_s']}s")
    print(f"steps executed: {s['steps_executed']}  replayed from checkpoint: {s['steps_replayed']}  "
          f"faults injected: {s['faults_injected'] or 'none'}")
    for bid, b in s["briefs"].items():
        if b.get("infeasible"):
            print(f"  {bid} INFEASIBLE, no variants generated: {'; '.join(b['infeasible_reasons'])}")
            continue
        flag = " CONFLICT" if b["conflict_detected"] else ""
        print(f"  {bid}{flag}: ceiling CNY {b['cost_ceiling_cny']} | {b['statuses']} | shortlist {b['shortlist']}")
        if b.get("quality_paused"):
            print(f"  {bid} PAUSED by the quality breaker: {b['quality_paused']} "
                  f"(after review: `resume <run> --ignore-quality-breaker`, or fix the prompt/model in a new run)")


# run: config file, then CLI flags on top, then go
def cmd_run(a: argparse.Namespace) -> None:
    from .orchestrator.runner import execute_run

    cfg = load_config(a.config, variants_per_brief=a.variants, auto_approve=a.auto_approve)
    if a.interactive:
        cfg = cfg.model_copy(update={"interactive": True})
    if a.no_memory:
        cfg = cfg.model_copy(update={"use_memory": False})
    if a.no_faults:
        cfg = cfg.model_copy(update={"faults": cfg.faults.model_copy(update={"enabled": False})})
    if a.provider:
        cfg = cfg.with_provider(a.provider)
    if a.live:
        cfg = cfg.for_live()
    briefs = load_briefs(a.briefs, a.brief_ids)
    run_id = a.run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{cfg.name}"
    store = store_for(a.workspace)
    summary = asyncio.run(execute_run(cfg, store, Path(a.workspace), run_id, briefs, crash_after=a.crash_after_steps,
                                      console=not a.quiet, verbose=a.verbose, learn=a.learn))
    print_summary(summary)
    print(f"\ntodos:\n{_todos(store, run_id)}")
    print(f"\nartifacts: {Path(a.workspace) / 'runs' / run_id}")
    print(f"design report (the final output): {Path(a.workspace) / 'runs' / run_id / 'report.md'}")


def cmd_resume(a: argparse.Namespace) -> None:
    from .orchestrator.runner import execute_run

    store = store_for(a.workspace)
    run = store.get_run(a.run_id)
    if run is None:
        raise SystemExit(f"unknown run {a.run_id}")
    # resume uses the config and briefs saved with the run, not whatever is on disk now
    cfg = RunConfig.model_validate(run["config"])
    # a person looked at a paused brief and decided to finish it anyway
    if a.ignore_quality_breaker:
        cfg = cfg.model_copy(update={"quality_breaker": False})
        print("quality breaker disabled for this resume (designer override)")
    briefs = [DesignBrief.model_validate(b) for b in run["briefs"]]
    summary = asyncio.run(execute_run(cfg, store, Path(a.workspace), a.run_id, briefs, console=not a.quiet,
                                      verbose=a.verbose, learn=a.learn))
    print_summary(summary)
    print(f"\ntodos:\n{_todos(store, a.run_id)}")


def _todos(store: Store, run_id: str) -> str:
    from .orchestrator.todos import TodoList
    from .observability.tracing import NullTracer

    return TodoList(store, run_id, NullTracer()).render()


def cmd_todos(a: argparse.Namespace) -> None:
    store = store_for(a.workspace)
    print(_todos(store, a.run_id) or "(no todos)")
    for v in store.get_variants(a.run_id):
        r = v["result"] or {}
        print(f"  {v['variant_id']:<10} {v['status']:<17} {r.get('reason') or ''}")


def cmd_trace(a: argparse.Namespace) -> None:
    path = Path(a.workspace) / "runs" / a.run_id / "trace.jsonl"
    print(render_tree(load_trace(path), a.variant) or "(no spans)")


# a saved variant JSON file, one variant of a run, or the list of a run's variants
def cmd_show(a: argparse.Namespace) -> None:
    from .show import list_variants, render_variant

    if Path(a.target).is_file():
        print(render_variant(json.loads(Path(a.target).read_text())))
        return
    store = store_for(a.workspace)
    if a.variant_id is None:
        print(list_variants(store.get_variants(a.target)) or f"(no variants for run {a.target})")
        return
    v = store.get_variant(a.target, a.variant_id)
    if v is None:
        raise SystemExit(f"unknown variant {a.variant_id} in run {a.target}")
    print(render_variant(v))


# rebuild report.md from a saved run folder, e.g. one of the live eval runs
def cmd_report(a: argparse.Namespace) -> None:
    from .design_report import build_from_folder

    folder = Path(a.folder)
    if not (folder / "summary.json").exists():
        folder = Path(a.workspace) / "runs" / a.folder  # a run id instead of a path
    briefs = {b.brief_id: {"title": b.title, "text": b.text} for b in load_briefs()}
    print(build_from_folder(folder, briefs))


def cmd_runs(a: argparse.Namespace) -> None:
    for r in store_for(a.workspace).list_runs():
        print(f"{r['run_id']:<48} {r['status']:<10} {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created_at']))}")


def cmd_pending(a: argparse.Namespace) -> None:
    from .orchestrator import hitl

    for v in hitl.pending(store_for(a.workspace), a.run_id):
        r = v["result"] or {}
        cost = (r.get("cost") or {}).get("total_cny")
        print(f"{v['variant_id']:<10} {v['status']:<17} BOM CNY {cost}  caveats={r.get('caveats') or []}")


def cmd_approve(a: argparse.Namespace) -> None:
    from .orchestrator import hitl

    err = hitl.decide(store_for(a.workspace), a.run_id, a.variant_id, approve=not a.reject, reviewer=a.reviewer,
                      note=a.note or "")
    if err:
        raise SystemExit(err)
    print(f"{a.variant_id}: {'rejected' if a.reject else 'approved -> production_ready'} by {a.reviewer}")


def cmd_teach(a: argparse.Namespace) -> None:
    lesson = LessonMemory(store_for(a.workspace)).teach(a.text, agent=a.agent, tags=a.tags or ["*"], rule_id=a.rule)
    print(f"stored {lesson.lesson_id} for agent '{lesson.agent}' tags={lesson.tags} rule={lesson.rule_id}")


def cmd_lessons(a: argparse.Namespace) -> None:
    store = store_for(a.workspace)
    if a.forget:
        store.delete_lesson(a.forget)
        print(f"deleted {a.forget}")
    for l in store.list_lessons():
        print(f"{l['lesson_id']:<22} {l['source']:<6} {l['agent']:<20} rule={l['rule_id']} tags={l['tags']}\n    {l['text']}")


def cmd_ask(a: argparse.Namespace) -> None:
    from .agents import explainer
    from .orchestrator.ask import retrieve_facts
    from .orchestrator.runner import build_runtime

    store = store_for(a.workspace)
    run = store.get_run(a.run_id)
    if run is None:
        raise SystemExit(f"unknown run {a.run_id}")
    cfg = RunConfig.model_validate(run["config"]).model_copy(
        # answering a question shouldn't inject faults or prompt for input
        update={"faults": RunConfig().faults, "interactive": False})
    rt = build_runtime(cfg, store, a.run_id, None, console=False)
    facts = retrieve_facts(store, a.run_id, a.question)
    res = asyncio.run(explainer.run(rt.ctx, a.question, facts))
    print(res.artifact.answer)
    if res.artifact.cited_variants:
        print("cited:", ", ".join(res.artifact.cited_variants))


def cmd_eval(a: argparse.Namespace) -> None:
    from .eval.harness import run_eval

    code = asyncio.run(run_eval(a))
    # only fail the process on a REVIEW/BLOCK verdict when --strict is given (CI)
    sys.exit(code if a.strict else 0)


def cmd_demo(a: argparse.Namespace) -> None:
    from .demo import run_demo

    run_demo(a)


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="creaition", description="cre[ai]tion multi-agent design pipeline")
    p.add_argument("--workspace", default="workspace", help="state directory (SQLite db, traces, artifacts)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the pipeline on briefs")
    r.add_argument("--config", help="config JSON (configs/*.json)")
    r.add_argument("--briefs", nargs="*", help="brief files or directories (default eval/briefs)")
    r.add_argument("--brief-ids", nargs="*", help="only these brief ids, e.g. B1 B5")
    r.add_argument("--variants", type=int, help="variants per brief")
    r.add_argument("--run-id")
    r.add_argument("--crash-after-steps", type=int, help="simulate kill -9 after N executed steps")
    r.add_argument("--interactive", action="store_true", help="Ask: put conflicts/questions to you on the terminal")
    r.add_argument("--live", action="store_true", help="call real models (key from env or .env)")
    r.add_argument("--provider", choices=["deepseek", "mixed"],
                   help="model preset: deepseek (default; DEEPSEEK_API_KEY) or mixed (DeepSeek + an OpenAI judge; "
                        "also needs OPENAI_API_KEY, OpenAI spend capped at CNY 3 per run)")
    r.add_argument("--auto-approve", choices=["never", "clean"])
    r.add_argument("--learn", action="store_true", help="distil lessons from this run into memory")
    r.add_argument("--no-memory", action="store_true")
    r.add_argument("--no-faults", action="store_true")
    r.add_argument("--verbose", action="store_true")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="resume a crashed/killed run from its checkpoints")
    rs.add_argument("run_id")
    rs.add_argument("--learn", action="store_true")
    rs.add_argument("--ignore-quality-breaker", action="store_true",
                    help="finish briefs the quality breaker paused (after a person has reviewed them)")
    rs.add_argument("--verbose", action="store_true")
    rs.add_argument("--quiet", action="store_true")
    rs.set_defaults(fn=cmd_resume)

    for name, fn, helptext in (("todos", cmd_todos, "show a run's todo list and variant statuses"),
                               ("pending", cmd_pending, "variants awaiting human approval")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("run_id")
        sp.set_defaults(fn=fn)

    t = sub.add_parser("trace", help="print the span tree of a run (optionally one variant)")
    t.add_argument("run_id")
    t.add_argument("--variant")
    t.set_defaults(fn=cmd_trace)

    sh = sub.add_parser("show", help="readable result of a variant (or list a run's variants)")
    sh.add_argument("target", help="a run id, or the path to a saved variant JSON file")
    sh.add_argument("variant_id", nargs="?", help="variant to show; leave out to list the run's variants")
    sh.set_defaults(fn=cmd_show)

    rp = sub.add_parser("report", help="rebuild a run's design report (report.md + sketches) from its folder")
    rp.add_argument("folder", help="a run folder with summary.json and variants/, or a run id in the workspace")
    rp.set_defaults(fn=cmd_report)

    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs)

    ap = sub.add_parser("approve", help="human approval gate")
    ap.add_argument("run_id")
    ap.add_argument("variant_id")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--reviewer", default="designer")
    ap.add_argument("--note")
    ap.set_defaults(fn=cmd_approve)

    te = sub.add_parser("teach", help="teach an agent a lesson (stored in long-term memory)")
    te.add_argument("text")
    te.add_argument("--agent", default="*", help="agent role, e.g. structural_modeler, concept_generator, reviser")
    te.add_argument("--tags", nargs="*", help="brief tags: indoor outdoor cost durability aesthetics sustainability sealed")
    te.add_argument("--rule", help="DFM rule id the lesson targets, e.g. RIB_RATIO")
    te.set_defaults(fn=cmd_teach)

    le = sub.add_parser("lessons", help="list (or --forget) lessons in memory")
    le.add_argument("--forget")
    le.set_defaults(fn=cmd_lessons)

    ak = sub.add_parser("ask", help="ask a question about a run")
    ak.add_argument("run_id")
    ak.add_argument("question")
    ak.set_defaults(fn=cmd_ask)

    ev = sub.add_parser("eval", help="evaluation harness + regression report")
    ev.add_argument("--baseline", default=str(ROOT / "configs" / "baseline.json"))
    ev.add_argument("--candidate", default=str(ROOT / "configs" / "candidate_prompt_v2.json"))
    ev.add_argument("--variants", type=int, default=6)
    ev.add_argument("--briefs", nargs="*")
    ev.add_argument("--out", default="reports")
    ev.add_argument("--benchmark", action="store_true", help="also run chaos robustness + memory-effect benchmark")
    ev.add_argument("--live", action="store_true")
    ev.add_argument("--provider", choices=["deepseek", "mixed"])
    ev.add_argument("--strict", action="store_true", help="exit non-zero when the gate blocks (for CI)")
    ev.set_defaults(fn=cmd_eval)

    de = sub.add_parser("demo", help="offline end-to-end showcase: faults, crash/resume, memory, eval")
    de.add_argument("--out", default="sample_outputs")
    de.set_defaults(fn=cmd_demo)

    # pick up API keys from .env before anything reads them
    load_dotenv()
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
