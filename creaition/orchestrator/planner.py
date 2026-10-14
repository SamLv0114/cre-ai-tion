"""Decides the next step for a variant, and whether a finished variant is production-ready.

Plain code on purpose: the flow is known in advance, has to replay identically on resume, and must not be
talked out of a guard. The revise loop stops on a revision cap, step cap, repeated spec, budget, or repeated
reviser failures; the variant then gets one try with the safe template, then a final decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.drift import concept_drift
from ..domain.flags import Degraded
from ..config import RunConfig
from ..domain.schemas import DesignContract, VariantStatus
from .state import GateDecision, Step, VariantState

# fallback flags that stop a variant from being auto-approved, with the text shown to the reviewer
CRITICAL_DEGRADATIONS = {
    Degraded.INTERPRETER_FALLBACK: "brief interpreted by deterministic fallback",
    Degraded.CONCEPT_FALLBACK: "concept from deterministic template",
    Degraded.STRUCTURAL_FALLBACK: "structure from deterministic baseline (modeler unavailable)",
    Degraded.LOOP_FALLBACK: "structure from deterministic baseline (revise loop exhausted)",
    Degraded.DFM_LOCAL: "DFM checked by local baseline engine only",
    Degraded.MATERIALS_SNAPSHOT: "material data from stale snapshot",
    Degraded.PRICE_SNAPSHOT: "prices from stale snapshot",
    Degraded.TOOLING_RATE_TABLE: "tooling cost from rate table, not a live quote",
    Degraded.JUDGE_UNAVAILABLE: "no quality judgement",
}


@dataclass(frozen=True)
class Decision:
    step: Step
    reason: str


# reason we must not start another LLM revision, or None if it's fine to continue
def _loop_blocker(s: VariantState, cfg: RunConfig) -> str | None:
    if s.fallback_used:
        return "already on deterministic fallback"
    if s.llm_revisions >= cfg.max_revisions:
        return f"revision cap reached ({cfg.max_revisions})"
    if s.oscillation:
        return "oscillation: a previous spec reappeared"
    if s.budget_exhausted:
        return "variant token/cost budget exhausted"
    # keep ~30% of the budget so the judge can still run
    used = max(s.usage.total_tokens / cfg.variant_token_budget, s.usage.cost_cny / cfg.variant_cost_budget_cny)
    if used > 0.7:
        return f"{used:.0%} of variant budget used; remainder reserved for the judge"
    if s.reviser_failures >= 2:
        return "reviser failed twice"
    return None


def next_step(s: VariantState, cfg: RunConfig) -> Decision:
    # the order of these checks is the order of the pipeline
    if s.status.terminal:
        return Decision(Step.DONE, "terminal")
    if s.steps >= cfg.max_steps_per_variant:
        return Decision(Step.FINALIZE, f"step guard ({cfg.max_steps_per_variant}) tripped")
    if s.step_errors >= 3:
        return Decision(Step.FINALIZE, "repeated step errors")
    if s.concept is None:
        return Decision(Step.GENERATE_CONCEPT, "no concept yet")
    if s.structure is None:
        return Decision(Step.BUILD_STRUCTURE, "no structural spec yet")
    if s.dfm is None:
        return Decision(Step.CHECK_DFM, f"structure r{s.structure.revision} not yet checked")

    if not s.dfm.passed:
        fails = s.dfm.failures
        auto = sorted({f.rule_id for f in fails if f.fix == "auto"})
        design = sorted({f.rule_id for f in fails if f.fix != "auto"})
        # cheap formula fixes first; don't autofix twice in a row
        if auto and s.structure_source != "autofix" and s.autofixes < 3:
            return Decision(Step.AUTO_FIX, f"closed-form fixes for {auto}" + (f"; {design} need design" if design else ""))
        blocker = _loop_blocker(s, cfg)
        if blocker is None:
            return Decision(Step.REVISE, f"design trade-off needed for {design or auto}")
        if not s.fallback_used:
            # loop is done: one try with the safe template, never back into the loop
            return Decision(Step.FALLBACK_BASELINE, f"{blocker}; DFM still failing {design or auto}")
        return Decision(Step.FINALIZE, f"{blocker}; DFM failing")

    if s.cost is None:
        return Decision(Step.ESTIMATE_COST, "DFM passed; cost next")
    if not s.cost.within_budget:
        blocker = _loop_blocker(s, cfg)
        over = s.cost.total_cny - s.cost.ceiling_cny
        if blocker is None:
            return Decision(Step.REVISE, f"over budget by CNY {over:.2f}")
        if not s.fallback_used:
            return Decision(Step.FALLBACK_BASELINE, f"{blocker}; over budget by CNY {over:.2f}")
        return Decision(Step.FINALIZE, f"{blocker}; over budget")

    if not s.judge_attempted:
        return Decision(Step.REVIEW_QUALITY, "all deterministic gates passed; judge quality")
    return Decision(Step.FINALIZE, "all checks complete")


def gate(s: VariantState, contract: DesignContract, cfg: RunConfig, reason: str) -> GateDecision:
    """Deterministic production-readiness gate (the definition of 'production-ready', minus human approval)."""
    # infrastructure trouble, not a design problem
    if s.step_errors >= 3 and (s.dfm is None or s.cost is None):
        return GateDecision(status=VariantStatus.FAILED, reason=f"infrastructure: {reason}")
    if s.dfm is None or not s.dfm.passed:
        rules = sorted({f.rule_id for f in s.dfm.failures}) if s.dfm else ["not checked"]
        return GateDecision(status=VariantStatus.REJECTED, reason=f"DFM failing {rules} ({reason})")
    if s.cost is None or not s.cost.within_budget:
        total = s.cost.total_cny if s.cost else float("nan")
        return GateDecision(status=VariantStatus.REJECTED,
                            reason=f"cost CNY {total:.2f} > ceiling CNY {contract.cost_ceiling_cny:g} ({reason})")
    # anything that passed only thanks to a fallback needs a human
    caveats = [CRITICAL_DEGRADATIONS[d] for d in s.degraded if d in CRITICAL_DEGRADATIONS]
    # the concept's look changed on the way to the final structure: a person should see that, whatever the judge says
    drift = concept_drift(s.concept, s.structure) if s.concept and s.structure else []
    if drift:
        caveats.append("concept features changed in the final structure (checked by code): " + "; ".join(drift))
    if contract.requires_human_resolution:
        caveats.append("brief had conflicts/assumptions resolved by defaults: " + "; ".join(contract.assumptions[:3]))
    if s.review is None:
        return GateDecision(status=VariantStatus.NEEDS_HUMAN, reason="deterministic gates passed, no judge score",
                            caveats=caveats)
    sc = s.review.scores
    # a good average can't hide one terrible score
    worst = min(sc.brief_adherence, sc.form_coherence, sc.cmf_fit, sc.usability, sc.intent_preservation)
    if s.review.overall < cfg.judge_accept_threshold or worst < cfg.judge_min_criterion:
        return GateDecision(status=VariantStatus.REJECTED,
                            reason=f"quality {s.review.overall:.2f} (min criterion {worst}) below bar "
                                   f"{cfg.judge_accept_threshold}/{cfg.judge_min_criterion}")
    if sc.brief_adherence < cfg.judge_min_adherence:
        return GateDecision(status=VariantStatus.REJECTED,
                            reason=f"off-brief: judge brief_adherence {sc.brief_adherence} < {cfg.judge_min_adherence} "
                                   f"({'; '.join(s.review.flags) or s.review.rationale[:80]})")
    if caveats:
        return GateDecision(status=VariantStatus.NEEDS_HUMAN, reason="passed with caveats", caveats=caveats)
    return GateDecision(status=VariantStatus.PENDING_APPROVAL, reason="all gates passed; awaiting approval")
