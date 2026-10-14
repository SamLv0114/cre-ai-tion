"""Model routing for one call: spend cap, circuit breaker, rate limit, in-flight limit, then retries with backoff.

If this model gives up, the caller (structured.py) moves on to the next model in the role's chain.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from ..config import ModelRouteCfg, RunConfig
from ..observability.tracing import Tracer
from ..reliability.circuit import BreakerRegistry
from ..reliability.errors import CircuitOpenError, LLMAuthError, LLMRateLimited, LLMTimeout
from ..reliability.limiter import AdaptiveLimiter, TokenBucket
from ..reliability.retry import retry_async
from .base import LLMClient, LLMRequest, LLMResponse


class ModelRouter:
    def __init__(self, cfg: RunConfig, clients: dict[str, LLMClient], breakers: BreakerRegistry, tracer: Tracer):
        self.cfg = cfg
        self.clients = clients
        self.breakers = breakers
        self.tracer = tracer
        self.policy = cfg.llm_retry.policy()
        self._buckets: dict[str, TokenBucket] = {}
        self.spend: dict[str, float] = {}  # CNY per provider in this run (enforces provider_spend_cap_cny)
        self._limiters: dict[str, AdaptiveLimiter] = {}

    # models to try for this agent, best first
    def chain(self, role: str) -> list[ModelRouteCfg]:
        return self.cfg.routes[role]

    # one rate limiter and one concurrency limiter per model, created on first use
    def _bucket(self, model: str) -> TokenBucket:
        if model not in self._buckets:
            self._buckets[model] = TokenBucket(self.cfg.model_rps, burst=max(1, int(self.cfg.model_rps)))
        return self._buckets[model]

    def limiter(self, model: str) -> AdaptiveLimiter:
        if model not in self._limiters:
            self._limiters[model] = AdaptiveLimiter(self.cfg.model_concurrency, maximum=self.cfg.model_concurrency * 4)
        return self._limiters[model]

    # price of one call from the token counts
    def cost_cny(self, model: str, resp: LLMResponse) -> float:
        p = self.cfg.pricing.get(model)
        if not p:
            return 0.0
        return round(resp.prompt_tokens / 1e6 * p.input_per_m + resp.completion_tokens / 1e6 * p.output_per_m, 6)

    async def complete(self, route: ModelRouteCfg, req: LLMRequest) -> LLMResponse:
        # vendor over its spend cap: behave like an open circuit so the caller moves on
        cap = self.cfg.provider_spend_cap_cny.get(route.provider)
        if cap is not None and self.spend.get(route.provider, 0.0) >= cap:
            self.tracer.event("guard", f"spend cap CNY {cap:g} reached for '{route.provider}'; skipping its routes")
            raise CircuitOpenError(f"spend cap reached for {route.provider}")
        breaker = self.breakers.get(f"llm:{route.provider}:{route.model}")
        probe = breaker.allow()
        client = self.clients[route.provider]
        bucket, limiter = self._bucket(route.model), self.limiter(route.model)

        # one try: wait for a rate-limit token, then for a free slot, then call
        async def attempt(n: int) -> LLMResponse:
            waited = await bucket.acquire()
            if waited > 0.05:
                self.tracer.event("backpressure", f"rate limiter held {route.model} call {waited * 1000:.0f} ms",
                                  console=False)
            async with limiter.slot():
                return await client.complete(replace(req, model=route.model, attempt=n, extra=route.params))

        def on_retry(n: int, exc: BaseException, delay: float) -> None:
            # a 429 means we're going too fast for this model
            if isinstance(exc, LLMRateLimited):
                limiter.on_throttle()
                self.tracer.event("throttle", f"429 from {route.model}: in-flight limit -> {limiter.limit}")
            self.tracer.event("retry", f"{route.model} attempt {n} failed ({type(exc).__name__}); "
                                       f"backoff {delay * 1000:.0f} ms", error=str(exc)[:160])

        try:
            resp = await retry_async(attempt, self.policy, timeout_exc=LLMTimeout, on_retry=on_retry)
        except asyncio.CancelledError:  # BaseException: not caught by 'except Exception' below
            if probe:
                breaker.release_probe()
            raise
        except LLMAuthError:
            # bad key won't fix itself; open the breaker now instead of after 3 failures
            breaker.record_failure(force_open=True)
            raise
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()
        limiter.on_success()
        self.spend[route.provider] = self.spend.get(route.provider, 0.0) + self.cost_cny(route.model, resp)
        return resp
