"""Content-addressed idempotency keys.

A step's key is a hash of everything that determines its output: run, variant,
step name, position in the variant's history, prompt version and a digest of
the typed input. The attempt number is deliberately excluded, so a retry or a
post-crash re-run of the same step maps to the same key and is served from the
checkpoint store instead of being re-billed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


# same data always gives the same string (sorted keys, no spaces)
def canonical(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# short sha256 of all parts; used for idempotency keys and cache keys
def digest(*parts: Any, length: int = 24) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(canonical(p).encode())
        h.update(b"\x1f")
    return h.hexdigest()[:length]


def stable_int(*parts: Any) -> int:
    """Deterministic integer seed (Python's hash() is salted per process)."""
    return int(digest(*parts, length=16), 16)
