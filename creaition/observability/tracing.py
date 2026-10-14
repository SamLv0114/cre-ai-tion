"""Per-step tracing as JSON lines: spans with timings, attributes and events, shaped like OpenTelemetry.

Interesting events are also printed with a tag ([FAULT], [RETRY], [REPAIR], ...) so one fault can be
followed from injection to recovery in the terminal.
"""

from __future__ import annotations

import contextvars
import json
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

current_span: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar("current_span", default=None)
current_variant: contextvars.ContextVar[str] = contextvars.ContextVar("current_variant", default="-")

CONSOLE_TAGS = {
    "fault.injected": "FAULT", "retry": "RETRY", "schema.invalid": "REPAIR", "repair.ok": "RECOVERED",
    "json.normalized": "RECOVERED", "fallback": "FALLBACK", "circuit": "CIRCUIT", "tool.degraded": "DEGRADED",
    "checkpoint.replay": "REPLAY", "guard": "GUARD", "plan": "PLAN", "variant.done": "DONE", "resume": "RESUME",
    "ask": "ASK", "todo": "TODO", "crash": "CRASH", "memory": "MEMORY", "cache.hit": "CACHE", "backpressure": "BACKPRESSURE",
    "run": "RUN", "throttle": "THROTTLE",
}


# one timed unit of work (run, brief, variant, step, model call, tool call)
@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    name: str
    variant: str
    start: float
    attrs: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    error: Optional[str] = None
    end: Optional[float] = None

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)

    def add_event(self, name: str, attrs: dict[str, Any]) -> None:
        self.events.append({"name": name, "t": round(time.time(), 4), **attrs})


class Tracer:
    def __init__(self, run_id: str, path: Optional[Path] = None, console: bool = True, verbose: bool = False,
                 console_log: Optional[Path] = None):
        self.run_id = run_id
        self.path = path
        self.console = console
        self.verbose = verbose
        self._lock = threading.Lock()
        self._fh = None
        self._log = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
        if console_log:
            console_log.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(console_log, "a", encoding="utf-8")

    # ------------------------------------------------------------------ spans
    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        parent = current_span.get()
        sp = Span(trace_id=self.run_id, span_id=uuid.uuid4().hex[:16], parent_id=parent.span_id if parent else None,
                  name=name, variant=current_variant.get(), start=time.time(), attrs=dict(attrs))
        # child spans started inside this block will point at this one as parent
        token = current_span.set(sp)
        try:
            yield sp
        except BaseException as exc:
            sp.status = "error"
            sp.error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            sp.end = time.time()
            current_span.reset(token)
            self._write({
                "type": "span", "trace_id": sp.trace_id, "span_id": sp.span_id, "parent_id": sp.parent_id,
                "name": sp.name, "variant": sp.variant, "start": round(sp.start, 4), "end": round(sp.end, 4),
                "duration_ms": round((sp.end - sp.start) * 1000, 2), "status": sp.status, "error": sp.error,
                "attrs": sp.attrs, "events": sp.events,
            })

    # ----------------------------------------------------------------- events
    def event(self, name: str, message: str = "", *, console: Optional[bool] = None, **attrs: Any) -> None:
        sp = current_span.get()
        payload = {"message": message, **attrs} if message else dict(attrs)
        if sp is not None:
            sp.add_event(name, payload)
        else:
            self._write({"type": "event", "trace_id": self.run_id, "name": name, "variant": current_variant.get(),
                         "t": round(time.time(), 4), **payload})
        # only the interesting events go to the terminal; everything goes to the file
        show = console if console is not None else (name in CONSOLE_TAGS or self.verbose)
        if self.console and show:
            self._print(name, message or json.dumps(attrs, default=str)[:200])

    def _print(self, name: str, message: str) -> None:
        tag = CONSOLE_TAGS.get(name, name.upper())
        ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        variant = current_variant.get()
        sp = current_span.get()
        where = sp.name if sp else "-"
        line = f"{ts} [{tag:<11}] {variant:<14} {where:<22} {message}"
        with self._lock:
            print(line, file=sys.stderr, flush=True)
            if self._log:
                self._log.write(line + "\n")
                self._log.flush()

    # one JSON object per line, flushed straight away so a crash doesn't lose it
    def _write(self, record: dict[str, Any]) -> None:
        if not self._fh:
            return
        line = json.dumps(record, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        if self._log:
            self._log.close()
            self._log = None


# tracer that records nothing (used by tests and small helpers)
class NullTracer(Tracer):
    def __init__(self) -> None:
        super().__init__("null", None, console=False)


def load_trace(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def render_tree(records: list[dict[str, Any]], variant: Optional[str] = None) -> str:
    """Pretty-print the span tree (optionally for one variant) for the `trace` command."""
    spans = [r for r in records if r.get("type") == "span" and (variant is None or r.get("variant") == variant)]
    by_parent: dict[Optional[str], list[dict]] = {}
    # group spans by parent; spans whose parent isn't in the list become roots
    ids = {s["span_id"] for s in spans}
    for s in spans:
        parent = s["parent_id"] if s["parent_id"] in ids else None
        by_parent.setdefault(parent, []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s["start"])
    out: list[str] = []

    def walk(parent: Optional[str], depth: int) -> None:
        for s in by_parent.get(parent, []):
            keys = ("model", "tool", "attempts", "repairs", "tokens", "cost_cny", "replayed", "degraded", "decision",
                    "status_out", "fallback_depth", "source")
            a = " ".join(f"{k}={s['attrs'][k]}" for k in keys if k in s["attrs"])
            flag = "" if s["status"] == "ok" else f"  !! {s['error']}"
            out.append(f"{'  ' * depth}- {s['name']} [{s['duration_ms']:.0f} ms] {a}{flag}")
            for e in s["events"]:
                msg = e.get("message") or {k: v for k, v in e.items() if k not in ("name", "t")}
                out.append(f"{'  ' * (depth + 1)}* {e['name']}: {msg}")
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    return "\n".join(out)
