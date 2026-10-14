"""SQLite store (WAL mode): runs, step checkpoints, variant statuses, todos, approvals, lessons and cache.

Every write commits straight away, so a killed process loses at most the step in flight. Step checkpoints
use INSERT OR IGNORE, so a re-run can't create duplicates. In production: Postgres + object storage + Redis.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at REAL, status TEXT, config_json TEXT, briefs_json TEXT, summary_json TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  idem_key TEXT PRIMARY KEY, run_id TEXT, variant_id TEXT, step TEXT, seq INTEGER,
  output_json TEXT, usage_json TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS steps_by_variant ON steps(run_id, variant_id, seq);
CREATE TABLE IF NOT EXISTS variants (
  run_id TEXT, variant_id TEXT, brief_id TEXT, status TEXT, result_json TEXT, updated_at REAL,
  PRIMARY KEY (run_id, variant_id)
);
CREATE TABLE IF NOT EXISTS todos (
  run_id TEXT, todo_id TEXT, seq INTEGER, title TEXT, status TEXT, detail TEXT, updated_at REAL,
  PRIMARY KEY (run_id, todo_id)
);
CREATE TABLE IF NOT EXISTS approvals (
  run_id TEXT, variant_id TEXT, decision TEXT, reviewer TEXT, note TEXT, at REAL,
  PRIMARY KEY (run_id, variant_id)
);
CREATE TABLE IF NOT EXISTS lessons (
  lesson_id TEXT PRIMARY KEY, agent TEXT, tags_json TEXT, rule_id TEXT, text TEXT, source TEXT,
  created_at REAL, weight REAL
);
CREATE TABLE IF NOT EXISTS cache (
  cache_key TEXT PRIMARY KEY, kind TEXT, vector_json TEXT, guard TEXT, value_json TEXT, created_at REAL,
  hits INTEGER DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # autocommit (isolation_level=None): every write is saved immediately
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # WAL keeps committed writes safe even if the process is killed
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    # one lock around the connection; callers never touch it directly
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ runs
    def create_run(self, run_id: str, config: dict, briefs: list[dict]) -> None:
        self._exec("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?)",
                   (run_id, time.time(), "running", json.dumps(config), json.dumps(briefs), None))

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        rows = self._all("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        return {"run_id": r["run_id"], "created_at": r["created_at"], "status": r["status"],
                "config": json.loads(r["config_json"]), "briefs": json.loads(r["briefs_json"]),
                "summary": json.loads(r["summary_json"]) if r["summary_json"] else None}

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        self._exec("UPDATE runs SET status=?, summary_json=? WHERE run_id=?", (status, json.dumps(summary), run_id))

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT run_id, created_at, status FROM runs ORDER BY created_at DESC")]

    # ----------------------------------------------------------------- steps
    # checkpoint lookup used by the executor before running a step
    def get_step(self, idem_key: str) -> Optional[dict[str, Any]]:
        rows = self._all("SELECT output_json, usage_json FROM steps WHERE idem_key=?", (idem_key,))
        if not rows:
            return None
        return {"output": json.loads(rows[0]["output_json"]), "usage": json.loads(rows[0]["usage_json"])}

    def put_step(self, idem_key: str, run_id: str, variant_id: str, step: str, seq: int, output: dict,
                 usage: dict) -> bool:
        # a second write with the same key is silently ignored: no duplicates
        cur = self._exec("INSERT OR IGNORE INTO steps VALUES (?,?,?,?,?,?,?,?)",
                         (idem_key, run_id, variant_id, step, seq, json.dumps(output), json.dumps(usage), time.time()))
        return cur.rowcount == 1

    def count_steps(self, run_id: str) -> int:
        return self._all("SELECT COUNT(*) AS n FROM steps WHERE run_id=?", (run_id,))[0]["n"]

    # -------------------------------------------------------------- variants
    # latest status/result per variant; on resume this is how finished variants are skipped
    def upsert_variant(self, run_id: str, variant_id: str, brief_id: str, status: str,
                       result: Optional[dict]) -> None:
        self._exec(
            "INSERT INTO variants VALUES (?,?,?,?,?,?) ON CONFLICT(run_id, variant_id) DO UPDATE SET "
            "status=excluded.status, result_json=excluded.result_json, updated_at=excluded.updated_at",
            (run_id, variant_id, brief_id, status, json.dumps(result) if result is not None else None, time.time()))

    def get_variants(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM variants WHERE run_id=? ORDER BY variant_id", (run_id,))
        return [{"variant_id": r["variant_id"], "brief_id": r["brief_id"], "status": r["status"],
                 "result": json.loads(r["result_json"]) if r["result_json"] else None} for r in rows]

    def get_variant(self, run_id: str, variant_id: str) -> Optional[dict[str, Any]]:
        for v in self.get_variants(run_id):
            if v["variant_id"] == variant_id:
                return v
        return None

    # ----------------------------------------------------------------- todos
    def upsert_todo(self, run_id: str, todo_id: str, seq: int, title: str, status: str, detail: str = "") -> None:
        self._exec(
            "INSERT INTO todos VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, todo_id) DO UPDATE SET "
            "title=excluded.title, status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at",
            (run_id, todo_id, seq, title, status, detail, time.time()))

    def get_todos(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT todo_id, seq, title, status, detail FROM todos WHERE run_id=? "
                                           "ORDER BY seq", (run_id,))]

    # ------------------------------------------------------------- approvals
    def set_approval(self, run_id: str, variant_id: str, decision: str, reviewer: str, note: str) -> None:
        self._exec("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,?)",
                   (run_id, variant_id, decision, reviewer, note, time.time()))

    def get_approvals(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {r["variant_id"]: dict(r) for r in self._all("SELECT * FROM approvals WHERE run_id=?", (run_id,))}

    # --------------------------------------------------------------- lessons
    def add_lesson(self, lesson_id: str, agent: str, tags: list[str], rule_id: Optional[str], text: str,
                   source: str, weight: float = 1.0) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO lessons VALUES (?,?,?,?,?,?,?,?)",
                         (lesson_id, agent, json.dumps(tags), rule_id, text, source, time.time(), weight))
        return cur.rowcount == 1

    def list_lessons(self, agent: Optional[str] = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM lessons", ()
        if agent:
            sql, params = sql + " WHERE agent=?", (agent,)
        rows = self._all(sql + " ORDER BY created_at", params)
        return [{**dict(r), "tags": json.loads(r["tags_json"])} for r in rows]

    def delete_lesson(self, lesson_id: str) -> None:
        self._exec("DELETE FROM lessons WHERE lesson_id=?", (lesson_id,))

    # ----------------------------------------------------------------- cache
    # count hits so cache usefulness shows up in the data
    def cache_get(self, key: str) -> Optional[dict[str, Any]]:
        rows = self._all("SELECT value_json FROM cache WHERE cache_key=?", (key,))
        if not rows:
            return None
        self._exec("UPDATE cache SET hits = hits + 1 WHERE cache_key=?", (key,))
        return json.loads(rows[0]["value_json"])

    def cache_put(self, key: str, kind: str, value: dict, vector: Optional[list[float]] = None,
                  guard: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,0)",
                   (key, kind, json.dumps(vector) if vector else None, guard, json.dumps(value), time.time()))

    def cache_scan(self, kind: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT cache_key, vector_json, guard, value_json FROM cache WHERE kind=?", (kind,))
        return [{"key": r["cache_key"], "vector": json.loads(r["vector_json"]) if r["vector_json"] else None,
                 "guard": r["guard"], "value": json.loads(r["value_json"])} for r in rows]
