"""Names of the 'degraded' flags a step can raise.

A flag means the step still produced a result, but through a fallback or with
stale data. Using these names instead of bare strings makes a typo fail loudly.
"""

from __future__ import annotations


class Degraded:
    # an LLM step fell back to a deterministic default
    INTERPRETER_FALLBACK = "interpreter_fallback"
    CONCEPT_FALLBACK = "concept_fallback"
    STRUCTURAL_FALLBACK = "structural_fallback"
    EXPLAINER_FALLBACK = "explainer_fallback"
    # the critic-and-revise loop gave up and used the safe template
    LOOP_FALLBACK = "loop_fallback_baseline"
    # a tool failed and we used local / stale data instead
    DFM_LOCAL = "dfm_local_baseline"
    MATERIALS_SNAPSHOT = "materials_snapshot"
    PRICE_SNAPSHOT = "price_snapshot"
    TOOLING_RATE_TABLE = "tooling_rate_table"
    # no usable answer at all from this step
    JUDGE_UNAVAILABLE = "judge_unavailable"
    REVISER_FAILED = "reviser_failed"
    REVISER_TOOL_CAP = "reviser_tool_loop_cap"
    BUDGET_EXHAUSTED = "budget_exhausted"

    # flags that mean "a model step was replaced by a template"
    FALLBACKS = frozenset({INTERPRETER_FALLBACK, CONCEPT_FALLBACK, STRUCTURAL_FALLBACK, EXPLAINER_FALLBACK})
