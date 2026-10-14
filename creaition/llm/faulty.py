"""Transport-level fault injection wrapper for any LLMClient (mock or live)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from ..observability.tracing import Tracer
from ..reliability.errors import LLMAuthError, LLMRateLimited, LLMServerError
from ..reliability.faults import FaultInjector, corrupt
from .base import LLMClient, LLMRequest, LLMResponse


class FaultyLLM:
    def __init__(self, inner: LLMClient, injector: FaultInjector, tracer: Tracer, hang_s: float = 30.0):
        self.inner = inner
        self.injector = injector
        self.tracer = tracer
        self.hang_s = hang_s
        self.provider = inner.provider

    async def complete(self, req: LLMRequest) -> LLMResponse:
        # ask the injector whether this call should fail, and how
        kind = self.injector.decide_llm(req.role, req.model, req.variant)
        if not kind:
            return await self.inner.complete(req)
        self.tracer.event("fault.injected", f"{kind} on {req.role} via {req.model} (attempt {req.attempt})",
                          kind=kind, model=req.model, role=req.role)
        if kind == "timeout":
            await asyncio.sleep(self.hang_s)  # cancelled by the per-attempt timeout
        if kind == "server_error":
            raise LLMServerError("injected HTTP 503", status=503)
        if kind == "rate_limit":
            raise LLMRateLimited("injected HTTP 429", status=429)
        if kind == "auth_error":
            raise LLMAuthError("injected HTTP 401", status=401)
        # malformed faults: make a real call, then damage the reply
        resp = await self.inner.complete(req)
        return replace(resp, text=corrupt(resp.text, kind))
