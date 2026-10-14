"""Human-in-the-loop approval gate.

No variant becomes PRODUCTION_READY without an approval record. The only
automatic path is an explicit policy (`auto_approve: "clean"`, used by the eval
harness), which approves PENDING_APPROVAL variants - those that passed every
gate with no degraded evidence and no unresolved brief conflict - and records
the policy as the reviewer. NEEDS_HUMAN variants always wait for a person.
"""

from __future__ import annotations

from typing import Optional

from ..domain.schemas import VariantStatus
from ..state.store import Store

# only these statuses can be approved or rejected by a person
APPROVABLE = {VariantStatus.PENDING_APPROVAL.value, VariantStatus.NEEDS_HUMAN.value}


# the 'clean' policy auto-approves fully clean variants; everything else waits
def apply_policy(store: Store, run_id: str, policy: str) -> int:
    if policy != "clean":
        return 0
    n = 0
    for v in store.get_variants(run_id):
        if v["status"] == VariantStatus.PENDING_APPROVAL.value:
            decide(store, run_id, v["variant_id"], approve=True, reviewer="auto-policy:clean",
                   note="all gates passed, no degraded evidence")
            n += 1
    return n


def decide(store: Store, run_id: str, variant_id: str, *, approve: bool, reviewer: str,
           note: str = "") -> Optional[str]:
    v = store.get_variant(run_id, variant_id)
    if v is None:
        return f"unknown variant {variant_id}"
    if v["status"] not in APPROVABLE:
        return f"{variant_id} is '{v['status']}', not awaiting approval"
    # variants with caveats can't be approved by a bot
    if v["status"] == VariantStatus.NEEDS_HUMAN.value and reviewer.startswith("auto"):
        return f"{variant_id} has caveats and requires a human reviewer"
    new = VariantStatus.PRODUCTION_READY if approve else VariantStatus.REJECTED
    result = dict(v["result"] or {})
    result["status"] = new.value
    result["reason"] = f"{'approved' if approve else 'rejected'} by {reviewer}" + (f": {note}" if note else "")
    # keep a record of who decided and why
    store.set_approval(run_id, variant_id, "approve" if approve else "reject", reviewer, note)
    store.upsert_variant(run_id, variant_id, v["brief_id"], new.value, result)
    return None


def pending(store: Store, run_id: str) -> list[dict]:
    return [v for v in store.get_variants(run_id) if v["status"] in APPROVABLE]
