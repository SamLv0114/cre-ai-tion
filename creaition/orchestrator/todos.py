"""Persistent run plan / todo list (long-horizon progress tracking).

Created deterministically at run start, updated as work completes, persisted
in the store so `todos <run_id>` shows progress of a live or crashed run and a
resumed run continues the same list instead of starting a new one.
"""

from __future__ import annotations

from ..config import RunConfig
from ..domain.schemas import DesignBrief
from ..observability.tracing import Tracer
from ..state.store import Store

# checkbox style used when printing the list
MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]", "paused": "[P]"}


class TodoList:
    def __init__(self, store: Store, run_id: str, tracer: Tracer):
        self.store = store
        self.run_id = run_id
        self.tracer = tracer

    def plan(self, briefs: list[DesignBrief], cfg: RunConfig) -> bool:
        """Create the plan unless one exists (resume). Returns True if newly created."""
        # a resumed run keeps its existing list
        if self.store.get_todos(self.run_id):
            return False
        items = [("plan", f"Plan run: {len(briefs)} brief(s) x {cfg.variants_per_brief} variant(s), config "
                          f"'{cfg.name}'")]
        for b in briefs:
            items += [(f"{b.brief_id}:interpret", f"{b.brief_id}: interpret brief"),
                      (f"{b.brief_id}:resolve", f"{b.brief_id}: resolve constraints, ask/adopt defaults"),
                      (f"{b.brief_id}:fanout", f"{b.brief_id}: generate + critique {cfg.variants_per_brief} variants")]
        items += [("approve", "Apply approval policy (human-in-the-loop gate)"),
                  ("shortlist", "Rank and shortlist per brief"), ("learn", "Distil lessons into memory"),
                  ("report", "Write run summary")]
        for seq, (tid, title) in enumerate(items):
            self.store.upsert_todo(self.run_id, tid, seq, title, "done" if tid == "plan" else "pending")
        self.tracer.event("todo", f"plan created with {len(items)} items")
        return True

    # update one item's status and detail text
    def set(self, todo_id: str, status: str, detail: str = "") -> None:
        for t in self.store.get_todos(self.run_id):
            if t["todo_id"] == todo_id:
                self.store.upsert_todo(self.run_id, todo_id, t["seq"], t["title"], status, detail)
                if status in ("done", "failed", "paused"):
                    self.tracer.event("todo", f"{MARK[status]} {t['title']}" + (f" - {detail}" if detail else ""),
                                      console=False)
                return

    def render(self) -> str:
        lines = []
        for t in self.store.get_todos(self.run_id):
            detail = f"  ({t['detail']})" if t["detail"] else ""
            lines.append(f"{MARK.get(t['status'], '[?]')} {t['title']}{detail}")
        return "\n".join(lines)
