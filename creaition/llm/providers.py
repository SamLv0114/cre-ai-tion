"""HTTP clients for the live models: DeepSeek (OpenAI- and Anthropic-compatible endpoints) and OpenAI.

Standard library only. Keys come from the environment or a git-ignored .env and are never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from ..reliability.errors import (
    LLMAuthError, LLMBadRequest, LLMError, LLMRateLimited, LLMServerError, LLMTimeout,
)
from .base import LLMRequest, LLMResponse, estimate_tokens


@dataclass(frozen=True)
class Endpoint:
    kind: str  # "openai" | "anthropic" (wire format)
    base_url: str
    key_envs: tuple[str, ...]
    max_tokens_field: str = "max_tokens"


ENDPOINTS: dict[str, Endpoint] = {
    "deepseek": Endpoint("openai", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",)),
    "deepseek_anthropic": Endpoint("anthropic", "https://api.deepseek.com/anthropic", ("DEEPSEEK_API_KEY",)),
    "openai": Endpoint("openai", "https://api.openai.com/v1", ("OPENAI_API_KEY",), "max_completion_tokens"),
}


# first env var that is set for this provider
def api_key_for(provider: str) -> Optional[str]:
    for env in ENDPOINTS[provider].key_envs:
        if os.environ.get(env):
            return os.environ[env].strip()
    return None


# map HTTP status to our error types (decides retry or not)
def _classify_http(status: int, body: str) -> LLMError:
    msg = f"HTTP {status}: {body[:300]}"
    if status in (401, 403):
        return LLMAuthError(msg, status=status)
    if status == 429:
        return LLMRateLimited(msg, status=status)
    if status >= 500:
        return LLMServerError(msg, status=status)
    return LLMBadRequest(msg, status=status)


# blocking HTTP call; run in a thread so it doesn't block the event loop
def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http(e.code, e.read().decode(errors="replace")) from None
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(getattr(e, "reason", None), TimeoutError) or isinstance(e, TimeoutError):
            raise LLMTimeout(str(e)) from None
        raise LLMServerError(f"network error: {e}") from None


class OpenAICompatClient:
    def __init__(self, provider: str, api_key: str, base_url: str, timeout_s: float = 120.0,
                 max_tokens_field: str = "max_tokens"):
        self.provider = provider
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_tokens_field = max_tokens_field
        # Optional params some models reject; dropped once on a 400 and remembered per model.
        self._unsupported: dict[str, set[str]] = {}

    async def complete(self, req: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": req.model, "messages": req.messages, "temperature": req.temperature,
            self.max_tokens_field: req.max_tokens,
        }
        # extras some models don't accept; dropped automatically on a 400
        optional: dict[str, Any] = {}
        if req.json_mode:
            optional["response_format"] = {"type": "json_object"}
        optional.update(req.extra)
        skip = self._unsupported.setdefault(req.model, set())
        body.update({k: v for k, v in optional.items() if k not in skip})
        headers = {"Authorization": f"Bearer {self._key}"}
        url = f"{self.base_url}/chat/completions"
        t0 = time.monotonic()
        try:
            data = await asyncio.to_thread(_post, url, headers, body, self.timeout_s)
        except LLMBadRequest:
            # model rejected an optional field: remember that and retry once without them
            if not optional or skip >= set(optional):
                raise
            skip.update(optional)
            for k in optional:
                body.pop(k, None)
            data = await asyncio.to_thread(_post, url, headers, body, self.timeout_s)
        latency = (time.monotonic() - t0) * 1000
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text, model=req.model, provider=self.provider,
            # use the provider's token counts; estimate only if they're missing
            prompt_tokens=int(usage.get("prompt_tokens") or estimate_tokens(json.dumps(req.messages))),
            completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(text)),
            latency_ms=latency, raw=None,
        )


class AnthropicCompatClient:
    def __init__(self, provider: str, api_key: str, base_url: str, timeout_s: float = 120.0):
        self.provider = provider
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def complete(self, req: LLMRequest) -> LLMResponse:
        # Anthropic format takes the system prompt as a separate field
        system = "\n\n".join(m["content"] for m in req.messages if m["role"] == "system")
        msgs = [m for m in req.messages if m["role"] != "system"]
        body = {"model": req.model, "max_tokens": req.max_tokens, "temperature": req.temperature,
                "system": system, "messages": msgs}
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
        t0 = time.monotonic()
        data = await asyncio.to_thread(_post, f"{self.base_url}/v1/messages", headers, body, self.timeout_s)
        latency = (time.monotonic() - t0) * 1000
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text, model=req.model, provider=self.provider,
            prompt_tokens=int(usage.get("input_tokens") or estimate_tokens(json.dumps(req.messages))),
            completion_tokens=int(usage.get("output_tokens") or estimate_tokens(text)),
            latency_ms=latency, raw=None,
        )


# pick the right client class for a route; fail early if its key is missing
def live_client(provider: str, timeout_s: float = 120.0):
    """Build the client for a route provider id, or raise SystemExit naming the missing env var."""
    ep = ENDPOINTS[provider]
    key = api_key_for(provider)
    if not key:
        raise SystemExit(f"live mode: routes use provider '{provider}' but none of {', '.join(ep.key_envs)} is set "
                         f"(export it, or put it in a git-ignored .env file in the project root)")
    if ep.kind == "openai":
        return OpenAICompatClient(provider, key, ep.base_url, timeout_s, ep.max_tokens_field)
    return AnthropicCompatClient(provider, key, ep.base_url, timeout_s)
