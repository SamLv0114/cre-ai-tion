"""Long-term memory: lessons (taught by a person or learned from repeated first-draft failures) and a semantic
cache for brief interpretations.

Lessons are read once at run start, so variants in one run never depend on each other's timing. The cache
only reuses a result when the brief's numbers match exactly, not just the wording.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..domain.schemas import DesignIntent
from ..reliability.idempotency import digest
from .store import Store

_TOKEN = re.compile(r"[a-z0-9]+")


def embed(text: str, dims: int = 512) -> list[float]:
    """Hashing-trick unigram+bigram vector. Offline, deterministic, good enough for near-duplicate detection."""
    toks = _TOKEN.findall(text.lower())
    # words and word pairs, hashed into a fixed-size vector
    feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    vec = [0.0] * dims
    for f in feats:
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        # a second hash bit picks the sign, which keeps collisions from piling up
        vec[h % dims] += 1.0 if (h >> 9) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# vectors are already normalised, so the dot product is the cosine
def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Lesson(BaseModel):
    model_config = ConfigDict(frozen=True)

    lesson_id: str
    agent: str  # agent role or "*"
    tags: list[str] = Field(default_factory=list)
    rule_id: Optional[str] = None
    text: str
    source: str  # "human" | "auto"
    weight: float = 1.0


# short tags (indoor/outdoor, main priority, sealed) used to pick relevant lessons
def intent_tags(intent: DesignIntent) -> set[str]:
    p = intent.priorities
    dominant = max(("cost", p.cost), ("durability", p.durability), ("aesthetics", p.aesthetics),
                   ("sustainability", p.sustainability), key=lambda kv: kv[1])[0]
    tags = {intent.environment, dominant}
    if intent.ip_rating != "none":
        tags.add("sealed")
    return tags


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    lessons: tuple[Lesson, ...] = ()

    def for_agent(self, agent: str, tags: Iterable[str] = (), k: int = 4) -> list[Lesson]:
        tagset = set(tags)
        scored = []
        for l in self.lessons:
            # lesson must be for this agent (or any agent) and share a tag with the brief
            if l.agent not in ("*", agent):
                continue
            if l.tags and not (set(l.tags) & tagset) and "*" not in l.tags:
                continue
            # more matching tags rank higher; designer-taught lessons get a small bonus
            score = l.weight + len(set(l.tags) & tagset) + (0.5 if l.source == "human" else 0.0)
            scored.append((score, l.lesson_id, l))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [l for _, _, l in scored[:k]]


LESSON_TEXT = {
    "WALL_RANGE": "Keep every wall between 1.5 and 3.0 mm; slim premium shells tend to drift below 1.5 mm.",
    "DRAFT_MIN": "Set draft from the finish: textured faces need >= 2 deg, smooth faces >= 1 deg.",
    "RIB_RATIO": "Size ribs at 50% of the nominal wall (allowed 40-60%); thicker ribs cause sink marks.",
    "INNER_RADIUS": "Inner radius must be at least 0.5 x the wall thickness.",
    "VENT_OPEN_AREA": "Intake vents need >= 15% open area; 7 slots of 1.2 x 16 mm in a 20 x 20 mm zone gives ~34%.",
    "USB_CLEARANCE": "Size the USB-C opening at >= 10 x 4.3 mm so the receptacle keeps 0.5 mm clearance per side.",
    "UNDERCUT_DECLARED": "Every snap-fit undercut must declare its side action (slider/lifter) and tooling cost.",
    "PACKAGING_FIT": "Envelope height must be >= 22 mm + 2 x wall to fit the stacked PCB and battery.",
    "DISPLAY_WINDOW_MIN": "Display window must be at least 42 x 18 mm.",
}


class LessonMemory:
    def __init__(self, store: Store):
        self.store = store

    # a lesson typed in by a person through the `teach` command
    def teach(self, text: str, agent: str = "*", tags: Optional[list[str]] = None, rule_id: Optional[str] = None,
              source: str = "human", weight: float = 1.5) -> Lesson:
        lesson = Lesson(lesson_id="L-" + digest(agent, text, length=10), agent=agent, tags=tags or ["*"],
                        rule_id=rule_id, text=text, source=source, weight=weight)
        self.store.add_lesson(lesson.lesson_id, lesson.agent, lesson.tags, lesson.rule_id, lesson.text,
                              lesson.source, lesson.weight)
        return lesson

    def snapshot(self, enabled: bool = True) -> MemorySnapshot:
        if not enabled:
            return MemorySnapshot()
        return MemorySnapshot(lessons=tuple(
            Lesson(lesson_id=r["lesson_id"], agent=r["agent"], tags=r["tags"], rule_id=r["rule_id"], text=r["text"],
                   source=r["source"], weight=r["weight"]) for r in self.store.list_lessons()))

    def learn_from_first_drafts(self, first_pass_failures: list[list[str]], min_rate: float = 0.10,
                                min_count: int = 3) -> list[Lesson]:
        """Turn recurring first-draft DFM failures into lessons for the structural modeler."""
        n = len(first_pass_failures)
        if n == 0:
            return []
        # how many first drafts failed each rule (once per variant)
        counts = Counter(rule for fails in first_pass_failures for rule in set(fails))
        learned = []
        for rule, c in sorted(counts.items()):
            if rule in LESSON_TEXT and c >= min_count and c / n >= min_rate:
                lesson = Lesson(lesson_id="L-auto-" + rule.lower(), agent="structural_modeler", tags=["*"],
                                rule_id=rule, text=f"{LESSON_TEXT[rule]} (seen in {c}/{n} first drafts)",
                                source="auto", weight=1.0)
                # fixed id per rule, so the same lesson is never stored twice
                if self.store.add_lesson(lesson.lesson_id, lesson.agent, lesson.tags, lesson.rule_id, lesson.text,
                                         lesson.source, lesson.weight):
                    learned.append(lesson)
        return learned


class SemanticCache:
    def __init__(self, store: Store, threshold: float = 0.93):
        self.store = store
        self.threshold = threshold

    def lookup(self, kind: str, text: str, guard: str) -> tuple[Optional[dict], float]:
        vec = embed(text)
        best, best_sim = None, 0.0
        for row in self.store.cache_scan(kind):
            # different numbers in the brief: never reuse, however similar the text
            if row["guard"] != guard or not row["vector"]:
                continue
            sim = cosine(vec, row["vector"])
            if sim > best_sim:
                best, best_sim = row, sim
        if best and best_sim >= self.threshold:
            self.store.cache_get(best["key"])  # bump hit counter
            return best["value"], best_sim
        return None, best_sim

    def put(self, kind: str, text: str, guard: str, value: dict) -> None:
        self.store.cache_put(f"{kind}:{digest(text, guard)}", kind, value, embed(text), guard)
