"""Run configuration: routing table, budgets, guards, retry policies, fault profile.

A config is data (JSON in configs/), versioned with the prompts. The regression
harness compares two configs; anything that can change output quality lives here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .reliability.faults import FaultConfig
from .reliability.retry import RetryPolicy

# Route provider ids -> API surface + credential (see llm/providers.py):
#   deepseek / deepseek_anthropic : DeepSeek official API, two surfaces (DEEPSEEK_API_KEY)
#   openai                        : OpenAI official API (OPENAI_API_KEY), only used by the "mixed" preset
ProviderId = Literal["deepseek", "deepseek_anthropic", "openai"]


# one entry in a role's fallback chain
class ModelRouteCfg(BaseModel):
    model: str
    provider: ProviderId = "deepseek"
    params: dict = Field(default_factory=dict)  # provider-specific request fields, e.g. {"thinking": {"type": "disabled"}}


# CNY per million tokens
class PricingCfg(BaseModel):
    input_per_m: float
    output_per_m: float


class RetryCfg(BaseModel):
    max_attempts: int = 3
    base_delay_s: float = 0.02
    max_delay_s: float = 0.5
    timeout_s: float = 1.0

    def policy(self) -> RetryPolicy:
        return RetryPolicy(self.max_attempts, self.base_delay_s, self.max_delay_s, self.timeout_s)


_NO_THINK = {"thinking": {"type": "disabled"}}  # we want the JSON answer, not a reasoning trace


# shorthand for building a route list; DeepSeek routes get thinking switched off
def _r(*pairs: tuple[str, str]) -> list[ModelRouteCfg]:
    return [ModelRouteCfg(model=m, provider=p, params=dict(_NO_THINK) if p == "deepseek" else {}) for m, p in pairs]


# Default preset: DeepSeek official API. Primary model first, then fallbacks; the Anthropic-compatible surface is a
# second network path to the same model. One vendor means the judge can differ from the generator by *model*
# (v4-pro vs flash) but not by *family*; the "mixed" preset fixes that with a small OpenAI judge.
DEFAULT_ROUTES: dict[str, list[ModelRouteCfg]] = {
    # once per brief, amortised over the whole fan-out -> afford the strongest model
    "brief_interpreter": _r(("deepseek-v4-pro", "deepseek"), ("deepseek-flash", "deepseek"),
                            ("deepseek-v4-pro", "deepseek_anthropic")),
    # one call per variant and divergence matters more than precision -> fast, cheap model
    "concept_generator": _r(("deepseek-flash", "deepseek"), ("deepseek-v4-pro", "deepseek"),
                            ("deepseek-flash", "deepseek_anthropic")),
    # precise numeric structured output -> strongest model
    "structural_modeler": _r(("deepseek-v4-pro", "deepseek"), ("deepseek-flash", "deepseek"),
                             ("deepseek-v4-pro", "deepseek_anthropic")),
    # narrow edit task on a known spec -> cheap model first
    "reviser": _r(("deepseek-flash", "deepseek"), ("deepseek-v4-pro", "deepseek")),
    # a different model from the concept generator
    "quality_judge": _r(("deepseek-v4-pro", "deepseek"), ("deepseek-flash", "deepseek")),
    "explainer": _r(("deepseek-flash", "deepseek"), ("deepseek-v4-pro", "deepseek")),
}

# "mixed": same as default, but the judge comes from another model family (OpenAI gpt-4.1-mini, non-reasoning,
# ~CNY 0.01 per judge call) with DeepSeek as its fallback. OpenAI is used for nothing else.
MIXED_ROUTES: dict[str, list[ModelRouteCfg]] = {
    **DEFAULT_ROUTES,
    "quality_judge": _r(("gpt-4.1-mini", "openai"), ("deepseek-v4-pro", "deepseek")),
}

# List prices (CNY per 1M tokens) at 7.1 CNY/USD. DeepSeek: peak, cache-miss (conservative; off-peak is half,
# cache hits ~50x cheaper). Checked against the vendors' pricing pages on 2026-10-13.
DEFAULT_PRICING: dict[str, PricingCfg] = {
    "deepseek-flash": PricingCfg(input_per_m=2.13, output_per_m=8.52),  # $0.30 / $1.20
    "deepseek-v4-pro": PricingCfg(input_per_m=9.37, output_per_m=28.12),  # $1.32 / $3.96
    "gpt-4.1-mini": PricingCfg(input_per_m=2.84, output_per_m=11.36),  # $0.40 / $1.60
}
PROVIDER_PRESETS = {"deepseek": DEFAULT_ROUTES, "mixed": MIXED_ROUTES}


# every setting for one run; defaults here, overridden by a configs/*.json file, then by CLI flags
class RunConfig(BaseModel):
    name: str = "baseline"
    description: str = ""
    llm_mode: Literal["mock", "live"] = "mock"
    provider: Literal["deepseek", "mixed"] = "deepseek"
    prompt_version: Literal["v1", "v2"] = "v1"
    routes: dict[str, list[ModelRouteCfg]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_ROUTES.items()})
    pricing: dict[str, PricingCfg] = Field(default_factory=lambda: dict(DEFAULT_PRICING))

    # fan-out and backpressure
    variants_per_brief: int = 4
    variants_when_relaxed: int = 2  # briefs that need relaxing or have hard conflicts: a few examples, not a full fan-out
    max_concurrency: int = 8  # variants in flight
    queue_size: int = 16  # bounded work queue -> producer blocks when workers lag
    model_concurrency: int = 4  # initial in-flight calls per model (AIMD adapts)
    model_rps: float = 50.0

    # loop guards and budgets
    max_revisions: int = 3  # hard cap on LLM critic-and-revise turns per variant
    max_steps_per_variant: int = 18
    max_reviser_tool_calls: int = 3
    max_repairs: int = 2  # schema repair attempts per model before falling back
    variant_token_budget: int = 80_000
    variant_cost_budget_cny: float = 0.60  # sized on live DeepSeek list prices, ~3x the live mean
    run_cost_budget_cny: float = 50.0
    # hard per-vendor spend cap per run; once reached that vendor's routes are skipped (fall through to the next)
    provider_spend_cap_cny: dict[str, float] = Field(default_factory=lambda: {"openai": 3.0})
    run_health_min_variants: int = 8
    run_health_max_failure_rate: float = 0.5
    # brief-level quality breaker (orchestrator/quality.py): pause a brief whose recent variants keep failing.
    # Rates are compared against the lower end of a 95% interval, so a few unlucky variants can't trip it.
    quality_breaker: bool = True
    quality_window: int = 40  # recent finished variants per brief
    quality_min_variants: int = 10
    quality_max_reject_rate: float = 0.7
    quality_max_same_cause_rate: float = 0.6
    quality_max_fallback_rate: float = 0.5

    # reliability
    llm_retry: RetryCfg = Field(default_factory=lambda: RetryCfg(max_attempts=3, base_delay_s=0.02, max_delay_s=0.3,
                                                                 timeout_s=1.0))
    tool_retry: RetryCfg = Field(default_factory=lambda: RetryCfg(max_attempts=3, base_delay_s=0.01, max_delay_s=0.1,
                                                                  timeout_s=0.4))
    step_timeout_s: float = 20.0
    breaker_threshold: int = 3
    breaker_reset_s: float = 2.0
    mock_time_scale: float = 0.002  # mock latency is modelled in ms, slept at this scale

    # quality gates
    judge_accept_threshold: float = 3.5
    judge_min_criterion: int = 2
    judge_min_adherence: int = 4  # manufacturable but off-brief is not production-ready
    auto_approve: Literal["never", "clean"] = "never"
    shortlist_k: int = 3

    # memory and cache
    use_memory: bool = True
    use_cache: bool = True

    interactive: bool = False
    faults: FaultConfig = Field(default_factory=FaultConfig)

    # real API calls need real timeouts, gentler rates and a spend cap
    def for_live(self) -> "RunConfig":
        """Live providers need real timeouts; mock defaults are tuned for a fast offline run."""
        return self.model_copy(update={
            "llm_mode": "live", "mock_time_scale": 1.0, "step_timeout_s": 600.0,
            "llm_retry": RetryCfg(max_attempts=3, base_delay_s=1.0, max_delay_s=20.0, timeout_s=120.0),
            "tool_retry": RetryCfg(max_attempts=3, base_delay_s=0.2, max_delay_s=2.0, timeout_s=10.0),
            "breaker_reset_s": 30.0, "model_rps": 2.0, "model_concurrency": 2, "max_concurrency": 4,
            "run_cost_budget_cny": min(self.run_cost_budget_cny, 10.0),  # real money: stop scheduling past CNY 10
        })

    def with_provider(self, provider: str) -> "RunConfig":
        """Swap the whole routing table to a preset (prompts, guards and prices unchanged)."""
        routes = PROVIDER_PRESETS[provider]
        return self.model_copy(update={"provider": provider,
                                       "routes": {k: [r.model_copy() for r in v] for k, v in routes.items()}})

    # warn about setups we know are risky (judge == generator, missing prices)
    def validate_routing(self) -> list[str]:
        warnings = []
        gen = {r.model for r in self.routes.get("concept_generator", [])[:1]}
        judge = {r.model for r in self.routes.get("quality_judge", [])[:1]}
        if gen & judge:
            warnings.append("judge primary model equals concept generator primary (self-preference risk)")
        for role, chain in self.routes.items():
            for r in chain:
                if r.model not in self.pricing:
                    warnings.append(f"no pricing for {r.model} (role {role})")
        return warnings


def load_config(path: str | Path | None, **overrides) -> RunConfig:
    data = json.loads(Path(path).read_text()) if path else {}
    data.update({k: v for k, v in overrides.items() if v is not None})
    if "routes" in data:  # a config only lists the roles it re-routes
        # a config file only lists the roles it changes; the rest keep the defaults
        merged = {k: [r.model_dump() for r in v] for k, v in DEFAULT_ROUTES.items()}
        merged.update(data["routes"])
        data["routes"] = merged
    return RunConfig.model_validate(data)
