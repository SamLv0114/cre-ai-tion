"""Ask feature, user -> system: question answering over a run's records.

Retrieval is deterministic (filters over the store); the explainer model only
phrases an answer from the retrieved facts and must cite variant ids. If the
model is unavailable the facts themselves are returned.
"""

from __future__ import annotations

import re
from typing import Any

from ..state.store import Store

# variant ids like B2-v003
_VID = re.compile(r"\b[\w]+-v\d{3}\b")


# one line of plain facts about a variant, for the explainer
def _fact(v: dict[str, Any]) -> str:
    r = v.get("result") or {}
    cost = (r.get("cost") or {}).get("total_cny")
    review = r.get("review") or {}
    scores = review.get("scores") or {}
    overall = None
    if scores:
        overall = round((scores["brief_adherence"] * 2 + scores["form_coherence"] + scores["cmf_fit"]
                         + scores["usability"] + scores["intent_preservation"]) / 6, 2)
    concept = r.get("concept") or {}
    cmf = concept.get("cmf") or {}
    return (f"{v['variant_id']} [{v['brief_id']}] status={v['status']} reason='{r.get('reason')}' "
            f"concept='{concept.get('name')}' material={cmf.get('material')} finish={cmf.get('finish')} "
            f"first-draft DFM failures={r.get('first_dfm_failures')} llm_revisions={r.get('llm_revisions')} "
            f"autofixes={r.get('autofixes')} cost=CNY {cost} judge={overall} degraded={list(r.get('degraded') or [])} "
            f"caveats={list(r.get('caveats') or [])}")


# question keywords -> what kind of records to pull
REJECT_WORDS = ("reject", "fail", "why not", "didn't", "did not")
BEST_WORDS = ("best", "recommend", "shortlist", "top", "approve", "ready")
COST_WORDS = ("cost", "expensive", "cheap", "budget")
FAULT_WORDS = ("fault", "retry", "recover", "error", "outage")


def _mentions(q: str, words: tuple[str, ...]) -> bool:
    return any(w in q for w in words)


def _select(variants: list[dict], question: str, run: dict) -> list[dict]:
    """Narrow the variants to the ones the question is about."""
    q = question.lower()
    ids = set(_VID.findall(question))
    if ids:
        variants = [v for v in variants if v["variant_id"] in ids]
    elif _mentions(q, REJECT_WORDS):
        variants = [v for v in variants if v["status"] in ("rejected", "failed", "needs_human")]
    elif _mentions(q, BEST_WORDS):
        short = {vid for s in (run.get("summary") or {}).get("shortlist", {}).values() for vid in s}
        variants = [v for v in variants if v["variant_id"] in short] or variants
    # "B3" in the question narrows to that brief (if it matches anything)
    for b in re.findall(r"\b(B\d+\w*)\b", question):
        variants = [v for v in variants if v["brief_id"].lower().startswith(b.lower())] or variants
    if _mentions(q, COST_WORDS):
        variants = sorted(variants, key=lambda v: ((v.get("result") or {}).get("cost") or {}).get("total_cny") or 0,
                          reverse=True)
    return variants


def retrieve_facts(store: Store, run_id: str, question: str, limit: int = 12) -> list[str]:
    run = store.get_run(run_id)
    if run is None:
        return []
    facts = [_fact(v) for v in _select(store.get_variants(run_id), question, run)[:limit]]
    summary = run.get("summary") or {}
    if summary and _mentions(question.lower(), FAULT_WORDS):
        facts.insert(0, f"run faults injected={summary.get('faults_injected')} breakers={summary.get('breakers')} "
                        f"fallback_rate={summary.get('fallback_rate')}")
    return facts
