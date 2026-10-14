"""Tools the agents use: material prices, tooling quotes, the DFM validator, and a what-if pricer for the reviser.

The 'live' services are simulated in-process. Each has a fallback (price snapshot, rate table, local rules)
that is used when it fails, and the result is flagged as degraded.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from ..domain import catalog as K
from ..domain import cost_model, dfm_rules
from ..domain import geometry as G
from ..domain.schemas import (
    DesignContract, DFMReport, Material, SecondaryOp, StructuralSpec,
)
from .registry import ToolExecutor, ToolResult


class Tools:
    def __init__(self, executor: ToolExecutor, latency_s: float = 0.002):
        self.ex = executor
        self.latency_s = latency_s

    async def materials(self, materials: Iterable[Material], variant: str) -> tuple[dict[Material, K.MaterialRecord], bool]:
        """Look up every material in one call. Returns (records, degraded)."""
        mats = sorted(set(materials), key=lambda m: m.value)

        # stands in for the PLM price service (same numbers, marked 'live')
        async def live() -> dict[Material, K.MaterialRecord]:
            await asyncio.sleep(self.latency_s)
            return {m: K.MATERIALS_SNAPSHOT[m].model_copy(update={"price_as_of": "live"}) for m in mats}

        res: ToolResult = await self.ex.invoke(
            "materials_lookup", live, variant=variant,
            # if the service fails: last known prices
            fallback=lambda: {m: K.MATERIALS_SNAPSHOT[m] for m in mats},
            fallback_desc="bundled price snapshot (stale prices flagged)")
        return res.value, res.degraded

    async def tooling_quote(self, spec: StructuralSpec, variant: str) -> tuple[cost_model.ToolingQuote, bool]:
        # stands in for a supplier quote service
        async def live() -> cost_model.ToolingQuote:
            await asyncio.sleep(self.latency_s)
            q = cost_model.tooling_quote_from_rates(spec)
            return q.model_copy(update={"source": "live"})

        res = await self.ex.invoke("tooling_quote", live, variant=variant,
                                   fallback=lambda: cost_model.tooling_quote_from_rates(spec),
                                   fallback_desc="internal tooling rate table")
        return res.value, res.degraded

    async def dfm_validate(self, spec: StructuralSpec, contract: DesignContract, densities: dict,
                           anchors: list[str], variant: str) -> tuple[DFMReport, bool]:
        async def live() -> DFMReport:
            await asyncio.sleep(self.latency_s)
            # full engine: required rules plus advisory checks
            return dfm_rules.evaluate(spec, contract, densities, anchors, full=True)

        res = await self.ex.invoke(
            "dfm_validate", live, variant=variant,
            # local engine: required rules only, flagged as reduced confidence
            fallback=lambda: dfm_rules.evaluate(spec, contract, densities, anchors, full=False),
            fallback_desc="local baseline rule set (advisory checks skipped, confidence=reduced)")
        return res.value, res.degraded


def cost_what_if(spec: StructuralSpec, contract: DesignContract, args: dict) -> dict:
    """Price a hypothetical change without committing to it. Pure function, no I/O."""
    parts = []
    material = args.get("material")
    drop = set(args.get("drop_secondary_ops") or [])
    # apply the hypothetical change to a copy, then price it; nothing is saved
    for p in spec.parts:
        if args.get("remove_bumper") and p.role == "bumper":
            continue
        upd = {"secondary_ops": [o for o in p.secondary_ops if o.value not in drop]}
        if material in {m.value for m in Material}:
            upd["material"] = Material(material)
        parts.append(p.model_copy(update=upd))
    trial = spec.model_copy(update={"parts": parts})
    quote = cost_model.tooling_quote_from_rates(trial)
    report = cost_model.estimate(trial, K.MATERIALS_SNAPSHOT, quote, contract.cost_ceiling_cny)
    dens = {m: r.density_g_cm3 for m, r in K.MATERIALS_SNAPSHOT.items()}
    return {"what_if": args, "total_cny": report.total_cny, "ceiling_cny": contract.cost_ceiling_cny,
            "within_budget": report.within_budget, "product_mass_g": G.product_mass_g(trial, dens),
            "valid_secondary_ops": [o.value for o in SecondaryOp]}


# reviser tool: plain material data for one resin
def materials_info(material: str) -> dict:
    try:
        rec = K.MATERIALS_SNAPSHOT[Material(material)]
    except ValueError:
        return {"error": f"unknown material '{material}'", "known": [m.value for m in Material]}
    return rec.model_dump(mode="json")
