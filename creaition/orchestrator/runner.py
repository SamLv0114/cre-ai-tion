"""Runs a whole job: prepare each brief once, fan its variants out to a bounded worker pool, then fan in
(approval policy, shortlist, lessons, summary).

Each variant is isolated: a failure becomes its status, never an exception in the pool. A run-level
breaker and run budget stop scheduling new variants when something is badly wrong.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..domain.flags import Degraded
from ..agents import concept as concept_agent
from ..agents import cost as cost_agent
from ..agents import critic, interpreter, judge, resolver, reviser, structural
from ..agents.base import AgentContext, AgentResult
from ..config import RunConfig
from ..design_report import build_report
from ..domain.baseline import baseline_structure
from ..domain.schemas import DesignBrief, DesignContract, DesignIntent, Usage, VariantStatus
from ..llm.providers import live_client
from ..llm.faulty import FaultyLLM
from ..llm.mock import MockLLM
from ..llm.router import ModelRouter
from ..observability.tracing import Tracer, current_variant
from ..prompts import prompt_hash
from ..reliability.circuit import BreakerRegistry
from ..reliability.faults import FaultInjector
from ..state.memory import LessonMemory, SemanticCache
from ..state.store import Store
from ..tools.registry import ToolExecutor
from ..tools.services import Tools
from . import hitl
from .quality import BriefQuality
from .executor import CrashHook, StepExecutor
from .planner import gate, next_step
from .state import Step, VariantState, apply
from .todos import TodoList


# per-run wiring, built once and shared by every brief and variant
@dataclass
class Runtime:
    cfg: RunConfig
    store: Store
    tracer: Tracer
    injector: FaultInjector
    breakers: BreakerRegistry
    router: ModelRouter
    ctx: AgentContext
    executor: StepExecutor
    run_id: str
    paused_briefs: dict[str, str] = field(default_factory=dict)  # filled by the quality breaker


# result of the brief-level steps; contract is None if the brief couldn't be prepared
@dataclass
class BriefPlan:
    brief: DesignBrief
    contract: Optional[DesignContract]
    usage: Usage = field(default_factory=Usage)
    degraded: list[str] = field(default_factory=list)


def build_clients(cfg: RunConfig, injector: FaultInjector, tracer: Tracer) -> dict:
    # only create clients for providers the routes actually use
    used = sorted({r.provider for chain in cfg.routes.values() for r in chain})
    if cfg.llm_mode == "live":
        base = {p: live_client(p, timeout_s=cfg.llm_retry.timeout_s) for p in used}
    else:
        mock = MockLLM(cfg.mock_time_scale)
        base = {p: mock for p in used}  # same behaviour, separate breakers per provider route
    return {name: FaultyLLM(client, injector, tracer) for name, client in base.items()}


def build_runtime(cfg: RunConfig, store: Store, run_id: str, trace_path: Optional[Path], *, console: bool = True,
                  verbose: bool = False, crash_after: Optional[int] = None,
                  console_log: Optional[Path] = None) -> Runtime:
    tracer = Tracer(run_id, trace_path, console=console, verbose=verbose, console_log=console_log)
    injector = FaultInjector(cfg.faults)
    breakers = BreakerRegistry(cfg.breaker_threshold, cfg.breaker_reset_s,
                               on_transition=lambda n, o, new: tracer.event("circuit", f"breaker {n}: {o} -> {new}"))
    router = ModelRouter(cfg, build_clients(cfg, injector, tracer), breakers, tracer)
    tools = Tools(ToolExecutor(cfg.tool_retry.policy(), injector, breakers, tracer),
                  latency_s=0.002 if cfg.llm_mode == "mock" else 0.05)
    # lessons are read once here and stay fixed for the whole run
    memory = LessonMemory(store).snapshot(cfg.use_memory)
    cache = SemanticCache(store) if cfg.use_cache else None
    ctx = AgentContext(cfg=cfg, router=router, tools=tools, tracer=tracer, memory=memory, semantic_cache=cache,
                       run_id=run_id)
    executor = StepExecutor(cfg, store, tracer, CrashHook(crash_after, tracer))
    return Runtime(cfg, store, tracer, injector, breakers, router, ctx, executor, run_id)


# ---------------------------------------------------------------------------
# brief level
# ---------------------------------------------------------------------------
async def prepare_brief(rt: Runtime, brief: DesignBrief, todos: TodoList) -> BriefPlan:
    # brief steps are checkpointed like variant steps, under a '<brief>::brief' scope
    cfg, scope = rt.cfg, f"{brief.brief_id}::brief"
    current_variant.set(scope)
    with rt.tracer.span("brief", brief_id=brief.brief_id):
        todos.set(f"{brief.brief_id}:interpret", "in_progress")
        out = await rt.executor.run(
            run_id=rt.run_id, scope=scope, seq=0, step=Step.INTERPRET_BRIEF,
            inputs=(brief, prompt_hash("brief_interpreter", cfg.prompt_version), cfg.routes["brief_interpreter"]),
            fn=lambda _b: interpreter.run(rt.ctx, brief), used=Usage())
        if out.artifact is None:
            todos.set(f"{brief.brief_id}:interpret", "failed", out.error or "")
            return BriefPlan(brief, None, out.usage, out.degraded)
        intent = DesignIntent.model_validate(out.artifact)
        todos.set(f"{brief.brief_id}:interpret", "done", f"{len(intent.conflicts)} conflict(s) flagged by model")

        # resolve is plain code; clarify may stop and ask the designer
        async def resolve_fn(_b) -> AgentResult:
            contract = resolver.resolve(brief, intent)
            contract = resolver.clarify(contract, rt.tracer, interactive=cfg.interactive)
            return AgentResult(artifact=contract)

        out2 = await rt.executor.run(run_id=rt.run_id, scope=scope, seq=1, step=Step.RESOLVE_CONTRACT,
                                     inputs=(intent, cfg.interactive), fn=resolve_fn, used=out.usage,
                                     human_in_loop=cfg.interactive)
        if out2.artifact is None:
            todos.set(f"{brief.brief_id}:resolve", "failed", out2.error or "")
            return BriefPlan(brief, None, out.usage + out2.usage, out.degraded)
        contract = DesignContract.model_validate(out2.artifact)
        todos.set(f"{brief.brief_id}:resolve", "done",
                  f"ceiling CNY {contract.cost_ceiling_cny:g}, floor CNY {contract.floor_cost_cny:.2f}, "
                  f"{len(contract.relaxations)} relaxation(s), human resolution "
                  f"{'required' if contract.requires_human_resolution else 'not needed'}")
        if contract.conflict_detected:
            rt.tracer.event("ask", f"{brief.brief_id}: hard conflicts -> " + "; ".join(
                c.description for c in contract.conflicts if c.severity == "hard")[:300])
        return BriefPlan(brief, contract, out.usage + out2.usage, out.degraded)


def planned_variants(contract: DesignContract, cfg: RunConfig) -> int:
    """How many variants a brief gets. Generating thousands of variants for a brief that can't be met
    (or only after relaxing it) mostly re-proves the same thing, so those briefs get few or none."""
    if contract.infeasible:
        return 0
    # only conflicts proven by code cut the fan-out; a model's own 'hard' label still sends variants to a human
    if contract.relaxations or contract.rule_conflict_detected:
        return min(cfg.variants_per_brief, cfg.variants_when_relaxed)
    return cfg.variants_per_brief


def infeasibility_report(brief: DesignBrief, contract: DesignContract) -> str:
    """What gets written instead of variants when a brief can't be met."""
    lines = [f"# {brief.brief_id}: not feasible as written", "", f"> {brief.text}", "", "## Why", ""]
    lines += [f"- {r}" for r in contract.infeasible_reasons]
    lines += ["", "## What would make it feasible", ""]
    lines += [f"- relax {r.field} from {r.strict_value:g} to {r.relaxed_value:g} ({r.reason})"
              for r in contract.relaxations if not r.accepted]
    lines += [f"- {b}: needs a change to the product (internals, materials) or to the platform limits"
              for b in contract.platform_blockers]
    lines += ["", "No variants were generated for this brief."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# variant level
# ---------------------------------------------------------------------------
# map a planned step to (the call to make, the inputs that go into its idempotency key)
def _bind(rt: Runtime, contract: DesignContract, s: VariantState, step: Step, reason: str):
    """Return (agent call, idempotency inputs) for a planned step."""
    ctx, cfg = rt.ctx, rt.cfg

    # the prompt text's hash goes into the key, so editing a prompt invalidates its checkpoints
    def p(role: str) -> str:
        return prompt_hash(role, cfg.prompt_version)

    # turn a plain value into an awaitable so code steps look like agent steps
    async def wrap(res: AgentResult) -> AgentResult:
        return res

    if step == Step.GENERATE_CONCEPT:
        return (lambda b: concept_agent.run(ctx, contract, s.direction, s.variant_id, b),
                (contract, s.direction, p("concept_generator"), cfg.routes["concept_generator"]))
    if step == Step.BUILD_STRUCTURE:
        return (lambda b: structural.run(ctx, contract, s.concept, b),
                (s.concept, p("structural_modeler"), cfg.routes["structural_modeler"]))
    if step == Step.CHECK_DFM:
        return (lambda b: critic.run_dfm(ctx, contract, s.concept, s.structure), (s.structure,))
    if step == Step.AUTO_FIX:
        return (lambda b: wrap(critic.autofix(s.structure, s.dfm)), (s.structure, s.dfm))
    if step == Step.REVISE:
        return (lambda b: reviser.run(ctx, contract, s.concept, s.structure, s.dfm, s.cost, b),
                (s.structure, s.dfm, s.cost, p("reviser"), cfg.routes["reviser"]))
    if step == Step.FALLBACK_BASELINE:
        rev = (s.structure.revision if s.structure else 0) + 1
        return (lambda b: wrap(AgentResult(artifact=baseline_structure(s.concept, contract, rev, reason),
                                           degraded=[Degraded.LOOP_FALLBACK], notes=[reason])),
                (s.concept, rev, reason))
    if step == Step.ESTIMATE_COST:
        return (lambda b: cost_agent.run(ctx, contract, s.structure), (s.structure, contract.cost_ceiling_cny))
    if step == Step.REVIEW_QUALITY:
        return (lambda b: judge.run(ctx, contract, s.concept, s.structure, s.cost, b),
                (s.structure, s.cost, p("quality_judge"), cfg.routes["quality_judge"]))
    if step == Step.FINALIZE:
        return (lambda b: wrap(AgentResult(artifact=gate(s, contract, cfg, reason))), (reason,))
    raise ValueError(step)


async def run_variant(rt: Runtime, plan: BriefPlan, idx: int, direction: dict) -> VariantState:
    vid = f"{plan.brief.brief_id}-v{idx + 1:03d}"
    # on resume, a finished variant is returned as-is
    existing = rt.store.get_variant(rt.run_id, vid)
    if (existing and existing["result"] and VariantStatus(existing["status"]).terminal
            and existing["status"] != VariantStatus.SKIPPED.value):  # skipped variants get another chance on resume
        return VariantState.model_validate({**existing["result"], "status": existing["status"]})
    current_variant.set(vid)
    contract = plan.contract
    s = VariantState(run_id=rt.run_id, variant_id=vid, brief_id=plan.brief.brief_id, direction=direction)
    rt.store.upsert_variant(rt.run_id, vid, s.brief_id, VariantStatus.RUNNING.value, None)
    with rt.tracer.span("variant", variant_id=vid, brief_id=s.brief_id, direction=direction["name"]) as sp:
        while True:
            # the variant loop: plan, execute (or replay), fold the result into a new state
            d = next_step(s, rt.cfg)
            if d.step == Step.DONE:
                break
            rt.tracer.event("plan", f"-> {d.step.value}: {d.reason}", console=rt.tracer.verbose)
            fn, inputs = _bind(rt, contract, s, d.step, d.reason)
            out = await rt.executor.run(run_id=rt.run_id, scope=vid, seq=s.steps, step=d.step, inputs=inputs,
                                        fn=fn, used=s.usage)
            s = apply(s, d.step, out)
        sp.set(status_out=s.status.value, revisions=s.llm_revisions, autofixes=s.autofixes, steps=s.steps,
               tokens=s.usage.total_tokens, cost_cny=round(s.usage.cost_cny, 5))
        rt.store.upsert_variant(rt.run_id, vid, s.brief_id, s.status.value, s.model_dump(mode="json"))
        cost = f"CNY {s.cost.total_cny:.2f}" if s.cost else "n/a"
        score = f"{s.review.overall:.2f}" if s.review else "n/a"
        rt.tracer.event("variant.done", f"{s.status.value.upper()} - {s.reason} | BOM {cost} | judge {score} | "
                                        f"{s.steps} steps, {s.llm_revisions} LLM rev, {s.autofixes} autofix | "
                                        f"{s.usage.total_tokens} tok")
    return s


# stops new variants from starting when too many fail or the run budget is gone
class RunHealth:
    """Run-level breaker: stop scheduling when infrastructure failures dominate."""

    def __init__(self, cfg: RunConfig, tracer: Tracer):
        self.cfg, self.tracer = cfg, tracer
        self.done = self.failed = 0
        self.cost = 0.0
        self.stop_reason: Optional[str] = None

    def record(self, s: VariantState) -> None:
        self.done += 1
        self.failed += s.status == VariantStatus.FAILED
        self.cost += s.usage.cost_cny
        if self.stop_reason:
            return
        if self.done >= self.cfg.run_health_min_variants and self.failed / self.done > self.cfg.run_health_max_failure_rate:
            self.stop_reason = f"run-health breaker: {self.failed}/{self.done} variants failed on infrastructure"
        elif self.cost > self.cfg.run_cost_budget_cny:
            self.stop_reason = f"run budget CNY {self.cfg.run_cost_budget_cny} exhausted"
        if self.stop_reason:
            self.tracer.event("guard", self.stop_reason + " -> no new variants will be scheduled")


async def fan_out(rt: Runtime, items: list[tuple[BriefPlan, int, dict]], todos: TodoList) -> list[VariantState]:
    cfg = rt.cfg
    # bounded queue + fixed worker pool = concurrency limit and backpressure
    queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_size)
    health = RunHealth(cfg, rt.tracer)
    quality = BriefQuality(cfg, rt.tracer)
    results: list[VariantState] = []
    per_brief_total = Counter(p.brief.brief_id for p, _, _ in items)
    per_brief_done: Counter = Counter()
    workers = max(1, min(cfg.max_concurrency, len(items)))

    async def producer() -> None:
        blocked = 0.0
        for item in items:
            t0 = time.monotonic()
            await queue.put(item)  # blocks while the queue is full -> backpressure on the planner
            blocked += time.monotonic() - t0
        for _ in range(workers):
            # one stop signal per worker
            await queue.put(None)
        if blocked > 0.01:
            rt.tracer.event("backpressure", f"producer blocked {blocked * 1000:.0f} ms on a full work queue "
                                            f"(size {cfg.queue_size}, {workers} workers)", console=False)

    async def worker() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            plan, idx, direction = item
            bid = plan.brief.brief_id
            # don't start new work once the run is unhealthy or this brief is paused; mark it skipped instead
            stop = health.stop_reason or (f"quality breaker: {quality.paused[bid]}" if bid in quality.paused else None)
            if stop:
                vid = f"{bid}-v{idx + 1:03d}"
                s = VariantState(run_id=rt.run_id, variant_id=vid, brief_id=bid, direction=direction,
                                 status=VariantStatus.SKIPPED, reason=stop)
                rt.store.upsert_variant(rt.run_id, vid, s.brief_id, s.status.value, s.model_dump(mode="json"))
            else:
                s = await run_variant(rt, plan, idx, direction)
            health.record(s)
            quality.record(s)
            results.append(s)
            per_brief_done[bid] += 1
            done = per_brief_done[bid] == per_brief_total[bid]
            if bid in quality.paused:
                rt.paused_briefs[bid] = quality.paused[bid]
                todos.set(f"{bid}:fanout", "paused", f"{per_brief_done[bid]}/{per_brief_total[bid]} variants; "
                                                     f"{quality.paused[bid]}")
            else:
                todos.set(f"{bid}:fanout", "done" if done else "in_progress",
                          f"{per_brief_done[bid]}/{per_brief_total[bid]} variants")

    await asyncio.gather(producer(), *(worker() for _ in range(workers)))
    return sorted(results, key=lambda s: s.variant_id)


# ---------------------------------------------------------------------------
# fan-in
# ---------------------------------------------------------------------------
# top-k per brief by judge score, cost headroom and DFM headroom
def shortlist(states: list[VariantState], k: int) -> list[str]:
    """Deterministic, diversity-aware ranking of variants that passed the automated gates."""
    ok = {VariantStatus.PENDING_APPROVAL, VariantStatus.PRODUCTION_READY, VariantStatus.NEEDS_HUMAN}
    cands = [s for s in states if s.status in ok and s.cost and s.review and s.dfm]

    def score(s: VariantState) -> float:
        return (0.5 * s.review.overall / 5 + 0.3 * max(0.0, s.cost.headroom_cny) / s.cost.ceiling_cny
                + 0.2 * max(0.0, s.dfm.headroom) - (0.2 if s.status == VariantStatus.NEEDS_HUMAN else 0.0))

    ranked = sorted(cands, key=score, reverse=True)
    picked: list[VariantState] = []
    seen: set[tuple] = set()
    # first pass: best variant of each look (silhouette + material family)
    for s in ranked:
        sig = (s.concept.silhouette, s.concept.cmf.material)
        if sig not in seen:
            picked.append(s)
            seen.add(sig)
    ids = [s.variant_id for s in picked[:k]]
    # second pass: fill any free slots with the best remaining variants, repeated looks included
    ids += [s.variant_id for s in ranked if s.variant_id not in ids][:k - len(ids)]
    return ids


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def summarize(rt: Runtime, plans: list[BriefPlan], states: list[VariantState], wall_s: float,
              lessons: list[str]) -> dict[str, Any]:
    # read final statuses from the store, since approvals may have changed them
    final = {v["variant_id"]: v["status"] for v in rt.store.get_variants(rt.run_id)}
    counts = Counter(final.values())
    ran = [s for s in states if s.status != VariantStatus.SKIPPED]
    n = max(1, len(ran))
    usage = sum((s.usage for s in states), Usage()) + sum((p.usage for p in plans), Usage())
    lat = [s.usage.latency_ms for s in ran]
    by_brief: dict[str, Any] = {}
    sl: dict[str, list[str]] = {}
    for p in plans:
        bs = [s for s in states if s.brief_id == p.brief.brief_id]
        sl[p.brief.brief_id] = shortlist(bs, rt.cfg.shortlist_k)
        c = p.contract
        by_brief[p.brief.brief_id] = {
            "title": p.brief.title,
            "conflict_detected": bool(c and c.conflict_detected),
            "requires_human_resolution": bool(c and c.requires_human_resolution),
            "cost_ceiling_cny": c.cost_ceiling_cny if c else None,
            "floor_cost_cny": c.floor_cost_cny if c else None,
            "assumptions": c.assumptions if c else [],
            "infeasible": bool(c and c.infeasible),
            "infeasible_reasons": c.infeasible_reasons if c else [],
            "variants_planned": planned_variants(c, rt.cfg) if c else 0,
            "quality_paused": rt.paused_briefs.get(p.brief.brief_id),
            "statuses": dict(Counter(final.get(s.variant_id, s.status.value) for s in bs)),
            "shortlist": sl[p.brief.brief_id],
        }
    faults = Counter(f["kind"] for f in rt.injector.injected)
    return {
        "run_id": rt.run_id, "config": rt.cfg.name, "llm_mode": rt.cfg.llm_mode,
        "prompt_version": rt.cfg.prompt_version, "variants": len(states), "status_counts": dict(counts),
        "production_ready": counts.get("production_ready", 0), "briefs": by_brief, "shortlist": sl,
        "first_pass_dfm_rate": round(sum(1 for s in ran if s.first_dfm_failures == []) / n, 3),
        "avg_llm_revisions": round(sum(s.llm_revisions for s in ran) / n, 3),
        "avg_autofixes": round(sum(s.autofixes for s in ran) / n, 3),
        "fallback_rate": round(sum(1 for s in ran if s.fallback_used or any(
            d in Degraded.FALLBACKS for d in s.degraded)) / n, 3),
        "tokens_total": usage.total_tokens, "llm_calls": usage.calls,
        "model_cost_cny_total": round(usage.cost_cny, 4),
        "model_cost_cny_per_variant": round(sum(s.usage.cost_cny for s in ran) / n, 5),
        "modeled_latency_ms_p50": round(percentile(lat, 0.5), 1), "modeled_latency_ms_p95": round(percentile(lat, 0.95), 1),
        "wall_clock_s": round(wall_s, 2),
        "faults_injected": dict(faults), "breakers": rt.breakers.snapshot(),
        "steps_executed": rt.executor.executed, "steps_replayed": rt.executor.replayed,
        "lessons_learned": lessons,
        "contracts": {p.brief.brief_id: p.contract.model_dump(mode="json") for p in plans if p.contract},
    }


async def execute_run(cfg: RunConfig, store: Store, workspace: Path, run_id: str, briefs: list[DesignBrief], *,
                      crash_after: Optional[int] = None, console: bool = True, verbose: bool = False,
                      learn: bool = False) -> dict[str, Any]:
    run_dir = workspace / "runs" / run_id
    # same run id as an existing run = resume
    resumed = store.get_run(run_id) is not None
    store.create_run(run_id, cfg.model_dump(mode="json"), [b.model_dump() for b in briefs])
    rt = build_runtime(cfg, store, run_id, run_dir / "trace.jsonl", console=console, verbose=verbose,
                       crash_after=crash_after, console_log=run_dir / "console.log")
    for w in cfg.validate_routing():
        rt.tracer.event("guard", f"config warning: {w}")
    todos = TodoList(store, run_id, rt.tracer)
    todos.plan(briefs, cfg)
    if resumed:
        rt.tracer.event("resume", f"resuming run {run_id}: {store.count_steps(run_id)} checkpointed steps on disk; "
                                  f"finished variants are skipped, in-flight ones replay from their last step")
    else:
        rt.tracer.event("run", f"run {run_id} config='{cfg.name}' mode={cfg.llm_mode} prompts={cfg.prompt_version} "
                               f"briefs={len(briefs)} variants/brief={cfg.variants_per_brief} "
                               f"faults={'on' if cfg.faults.enabled else 'off'}")
    t0 = time.monotonic()
    with rt.tracer.span("run", run_id=run_id, config=cfg.name):
        # interactive mode asks questions, so briefs go one at a time
        if cfg.interactive:
            plans = [await prepare_brief(rt, b, todos) for b in briefs]
        else:
            plans = list(await asyncio.gather(*(prepare_brief(rt, b, todos) for b in briefs)))
        items = []
        for p in plans:
            if p.contract is None:
                continue
            n = planned_variants(p.contract, cfg)
            if n == 0:
                bid = p.brief.brief_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / f"infeasible_{bid}.md").write_text(infeasibility_report(p.brief, p.contract))
                todos.set(f"{bid}:fanout", "skipped", "infeasible: " + "; ".join(p.contract.infeasible_reasons)[:200])
                rt.tracer.event("guard", f"{bid} is infeasible, no variants generated: "
                                         + "; ".join(p.contract.infeasible_reasons)[:240])
                continue
            if n < cfg.variants_per_brief:
                rt.tracer.event("guard", f"{p.brief.brief_id} needs relaxing / has hard conflicts: generating {n} "
                                         f"example variants instead of {cfg.variants_per_brief}")
            for i, d in enumerate(concept_agent.directions_for(p.contract, n)):
                items.append((p, i, d))
        current_variant.set("-")
        states = await fan_out(rt, items, todos)

        # fan-in: approval policy, lessons, summary
        approved = hitl.apply_policy(store, run_id, cfg.auto_approve)
        todos.set("approve", "done", f"{approved} auto-approved by policy '{cfg.auto_approve}'; "
                                     f"{len(hitl.pending(store, run_id))} awaiting a human")
        lessons: list[str] = []
        # new lessons are written now and only used by the next run
        if learn:
            learned = LessonMemory(store).learn_from_first_drafts(
                [s.first_dfm_failures for s in states if s.first_dfm_failures is not None])
            lessons = [l.text for l in learned]
            for l in learned:
                rt.tracer.event("memory", f"learned lesson for {l.agent}: {l.text}")
        todos.set("learn", "done" if learn else "skipped", f"{len(lessons)} new lesson(s)")
        summary = summarize(rt, plans, states, time.monotonic() - t0, lessons)
        todos.set("shortlist", "done", "; ".join(f"{b}: {', '.join(v) or '-'}" for b, v in summary["shortlist"].items()))
    store.finish_run(run_id, "completed", summary)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    # one JSON file per variant with everything it produced
    vdir = run_dir / "variants"
    variants = store.get_variants(run_id)
    if variants:
        vdir.mkdir(exist_ok=True)
    for v in variants:
        (vdir / f"{v['variant_id']}.json").write_text(json.dumps(v, indent=2))
    # the readable final output: one design card per shortlisted variant, with a sketch
    build_report(run_dir, summary, variants, {b.brief_id: {"title": b.title, "text": b.text} for b in briefs})
    todos.set("report", "done", str(run_dir / "summary.json"))
    rt.tracer.close()
    return summary
