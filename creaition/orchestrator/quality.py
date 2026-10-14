"""Brief-level quality breaker: pause a brief whose variants keep failing the same way.

RunHealth (runner.py) stops a run when *infrastructure* fails. This watches *outcomes*, per brief: if most of the
recent finished variants of one brief are rejected, mostly for the same reason, or only get through on the
deterministic fallback, the generation strategy is wrong for that brief, and generating the rest of its fan-out
only re-proves it at full price. The brief is paused (its remaining variants become SKIPPED) and handed to a
person. The run's config is never changed mid-run: a new prompt or model goes through the regression gate and a
new run.

First-draft DFM failures are deliberately not a signal. They are normal, and the critic loop exists to fix them
(live, 0% of first drafts passed and 100% of feasible variants were fixed).
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass

from ..config import RunConfig
from ..domain.flags import Degraded
from ..domain.schemas import VariantStatus
from ..observability.tracing import Tracer
from .state import VariantState

# statuses that say something about output quality (FAILED is infrastructure, RunHealth's job)
_OUTCOMES = {VariantStatus.REJECTED, VariantStatus.NEEDS_HUMAN, VariantStatus.PENDING_APPROVAL,
             VariantStatus.PRODUCTION_READY}


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Lower end of the 95% Wilson interval for k successes in n: a rate we are fairly sure is exceeded.
    Keeps a handful of unlucky variants from pausing a brief (10/10 -> 0.72, 7/10 -> 0.40)."""
    if n == 0:
        return 0.0
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def rejection_causes(s: VariantState) -> set[str]:
    """Why a variant was rejected, in coarse buckets. A DFM rejection counts once per failing rule."""
    if s.status != VariantStatus.REJECTED:
        return set()
    if s.dfm is not None and not s.dfm.passed:
        return {f"DFM {f.rule_id}" for f in s.dfm.failures}
    if s.cost is not None and not s.cost.within_budget:
        return {"over budget"}
    reason = s.reason or ""
    if reason.startswith("off-brief"):
        return {"off-brief (judge)"}
    if reason.startswith("quality"):
        return {"low quality (judge)"}
    return {"other"}


def used_fallback(s: VariantState) -> bool:
    return s.fallback_used or any(d in Degraded.FALLBACKS for d in s.degraded)


@dataclass(frozen=True)
class _Outcome:
    rejected: bool
    causes: frozenset[str]
    fallback: bool


class BriefQuality:
    """Sliding window of recent outcomes per brief; trips once per brief and stays tripped for the run."""

    def __init__(self, cfg: RunConfig, tracer: Tracer):
        self.cfg, self.tracer = cfg, tracer
        self.windows: dict[str, deque[_Outcome]] = {}
        self.paused: dict[str, str] = {}  # brief id -> why it was paused

    def record(self, s: VariantState) -> None:
        if not self.cfg.quality_breaker or s.status not in _OUTCOMES or s.brief_id in self.paused:
            return
        w = self.windows.setdefault(s.brief_id, deque(maxlen=self.cfg.quality_window))
        w.append(_Outcome(s.status == VariantStatus.REJECTED, frozenset(rejection_causes(s)), used_fallback(s)))
        if len(w) < self.cfg.quality_min_variants:
            return
        reason = self._check(w)
        if reason:
            self.paused[s.brief_id] = reason
            self.tracer.event("guard", f"quality breaker: {s.brief_id} paused ({reason}); its remaining variants "
                                       f"are skipped and the brief goes to a human")

    def _check(self, w: deque[_Outcome]) -> str | None:
        n = len(w)
        rejected = sum(o.rejected for o in w)
        causes = Counter(c for o in w for c in o.causes)
        top, top_n = causes.most_common(1)[0] if causes else ("", 0)
        fallback = sum(o.fallback for o in w)
        if wilson_lower(top_n, n) > self.cfg.quality_max_same_cause_rate:
            return f"{top_n}/{n} recent variants rejected for the same cause: {top}"
        if wilson_lower(rejected, n) > self.cfg.quality_max_reject_rate:
            return f"{rejected}/{n} recent variants rejected (most common cause: {top})"
        if wilson_lower(fallback, n) > self.cfg.quality_max_fallback_rate:
            return f"{fallback}/{n} recent variants only got through on the deterministic fallback"
        return None
