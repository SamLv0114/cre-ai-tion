"""Cost Estimator (deterministic should-cost over two tools)."""

from __future__ import annotations

from ..domain.flags import Degraded
from ..domain import cost_model
from ..domain.schemas import CostReport, DesignContract, StructuralSpec
from .base import AgentContext, AgentResult


async def run(ctx: AgentContext, contract: DesignContract, spec: StructuralSpec) -> AgentResult[CostReport]:
    variant = spec.variant_id
    # two tools: resin prices and a tooling quote; either may fall back to stale data
    prices, price_degraded = await ctx.tools.materials({p.material for p in spec.parts}, variant)
    quote, quote_degraded = await ctx.tools.tooling_quote(spec, variant)
    report = cost_model.estimate(spec, prices, quote, contract.cost_ceiling_cny,
                                 price_source="snapshot" if price_degraded else "live")
    # stale data still gives a number, but the variant will need a human to sign off
    degraded = ([Degraded.PRICE_SNAPSHOT] if price_degraded else []) + ([Degraded.TOOLING_RATE_TABLE] if quote_degraded else [])
    return AgentResult(artifact=report, degraded=degraded,
                       notes=[f"CNY {report.total_cny:.2f} / ceiling {report.ceiling_cny:g}"])
