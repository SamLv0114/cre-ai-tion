"""Runs one step: checkpoint lookup, timeout, budget, tracing, and the simulated-crash hook.

A step's key is a hash of run, variant, step, position and inputs (not the attempt number), so a retry or a
resumed run finds the saved result and doesn't call the model again.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel

from ..domain.flags import Degraded
from ..config import RunConfig
from ..domain.schemas import Usage
from ..observability.tracing import Tracer
from ..reliability.budget import Budget
from ..reliability.errors import BudgetExceeded
from ..reliability.idempotency import digest
from ..state.store import Store
from ..agents.base import AgentResult
from .state import Step, StepOutcome


# usage spent inside a step that failed part-way (budget.used minus what we started with)
def _delta(after: Usage, before: Usage) -> Usage:
    return Usage(prompt_tokens=after.prompt_tokens - before.prompt_tokens,
                 completion_tokens=after.completion_tokens - before.completion_tokens,
                 cost_cny=round(after.cost_cny - before.cost_cny, 6), calls=after.calls - before.calls,
                 latency_ms=after.latency_ms - before.latency_ms)


class CrashHook:
    """Simulates `kill -9` after N newly executed (non-replayed) steps, for the resume demo."""

    def __init__(self, crash_after: Optional[int], tracer: Tracer):
        self.remaining = crash_after
        self.tracer = tracer

    def tick(self) -> None:
        if self.remaining is None:
            return
        # count only newly executed steps; replayed ones don't move us toward the crash
        self.remaining -= 1
        if self.remaining <= 0:
            self.tracer.event("crash", "simulated hard kill (os._exit 137) - in-flight steps are lost, "
                                       "committed checkpoints survive")
            self.tracer.close()
            os._exit(137)


class StepExecutor:
    def __init__(self, cfg: RunConfig, store: Store, tracer: Tracer, crash: CrashHook):
        self.cfg = cfg
        self.store = store
        self.tracer = tracer
        self.crash = crash
        self.replayed = 0
        self.executed = 0

    async def run(self, *, run_id: str, scope: str, seq: int, step: Step, inputs: tuple,
                  fn: Callable[[Budget], Awaitable[AgentResult]], used: Usage,
                  human_in_loop: bool = False) -> StepOutcome:
        # same run, variant, step, position and inputs -> same key, whatever the attempt number
        key = digest(run_id, scope, step.value, seq, *inputs)
        with self.tracer.span(f"step.{step.value}", step=step.value, seq=seq, idem_key=key) as sp:
            # already done before (retry or resume): return the stored result, no model call
            cached = self.store.get_step(key)
            if cached is not None:
                self.replayed += 1
                out = StepOutcome.model_validate(cached["output"])
                sp.set(replayed=True, status_out="error" if out.error else "ok")
                self.tracer.event("checkpoint.replay", f"{step.value} #{seq} served from checkpoint {key[:10]} "
                                                       f"(no model call, no charge)", console=False)
                return out

            # budget starts from what this variant has already spent
            budget = Budget(self.cfg.variant_token_budget, self.cfg.variant_cost_budget_cny, used=used)
            try:
                # a step that waits on a person is not bound by the machine step timeout
                timeout = None if human_in_loop else self.cfg.step_timeout_s
                res = await asyncio.wait_for(fn(budget), timeout=timeout)
                art = res.artifact
                out = StepOutcome(
                    step=step, artifact_type=type(art).__name__ if isinstance(art, BaseModel) else None,
                    artifact=art.model_dump(mode="json") if isinstance(art, BaseModel) else None,
                    usage=res.usage, model=res.model, degraded=list(res.degraded), notes=list(res.notes),
                    # a failed revision or an unavailable judge is a legitimate outcome the planner handles,
                    # not a step error to retry
                    error=None if art is not None or step in (Step.REVISE, Step.REVIEW_QUALITY)
                    else "agent returned no artifact")
            except BudgetExceeded as exc:
                # any failure becomes an error result for the planner to handle; nothing escapes the variant
                self.tracer.event("guard", str(exc))
                out = StepOutcome(step=step, usage=_delta(budget.used, used), degraded=[Degraded.BUDGET_EXHAUSTED],
                                  error=str(exc))
            except asyncio.TimeoutError:
                self.tracer.event("guard", f"step {step.value} exceeded {self.cfg.step_timeout_s}s step timeout")
                out = StepOutcome(step=step, usage=_delta(budget.used, used), error="step timeout")
            except Exception as exc:  # noqa: BLE001 - bulkhead: a step failure must not escape the variant
                self.tracer.event("guard", f"step {step.value} raised {type(exc).__name__}: {exc}")
                out = StepOutcome(step=step, usage=_delta(budget.used, used),
                                  error=f"{type(exc).__name__}: {exc}"[:400])

            # commit before moving on, so a crash after this line loses nothing
            self.store.put_step(key, run_id, scope, step.value, seq, out.model_dump(mode="json"),
                                out.usage.model_dump())
            self.executed += 1
            sp.set(replayed=False, model=out.model, tokens=out.usage.total_tokens, cost_cny=out.usage.cost_cny,
                   degraded=out.degraded or None, status_out="error" if out.error else "ok",
                   notes="; ".join(out.notes)[:200] or None)
        self.crash.tick()
        return out
