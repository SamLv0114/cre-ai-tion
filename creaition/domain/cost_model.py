"""Deterministic should-cost model for the housing BOM.

Cost is arithmetic over a spec plus reference prices; there is no judgment call
an LLM could add. Tooling is amortised over ANNUAL_VOLUME x
TOOLING_AMORTIZATION_YEARS units, which is what makes side actions, 2K tools and
extra parts expensive at 10k units/year.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from . import catalog as K
from . import constants as C
from . import geometry as G
from .schemas import Closure, CostLine, CostReport, Material, Sealing, SecondaryOp, StructuralSpec


class ToolingQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    lines: list[CostLine]
    total_cny: float
    source: str  # "live" | "rate_table"


def tooling_quote_from_rates(spec: StructuralSpec) -> ToolingQuote:
    """Rate-table tooling quote. Also the fallback when the quote service is down."""
    lines: list[CostLine] = []
    for p in spec.parts:
        lines.append(CostLine(item=f"mould:{p.part_id}", cny=K.MOULD_BASE_CNY[p.role]))
        if p.finish.textured:
            lines.append(CostLine(item=f"texture:{p.part_id}", cny=K.TEXTURE_PER_PART_CNY))
        for u in p.undercuts:
            # An undeclared undercut is still physically there; quote it as a slider.
            mech = u.mechanism.value if u.mechanism else "slider"
            lines.append(CostLine(item=f"side_action:{u.undercut_id}:{mech}", cny=K.SIDE_ACTION_CNY[mech]))
        if SecondaryOp.TPE_OVERMOLD in p.secondary_ops:
            lines.append(CostLine(item=f"2k_tooling:{p.part_id}", cny=K.TPE_2K_TOOLING_CNY))
    return ToolingQuote(lines=lines, total_cny=round(sum(l.cny for l in lines), 2), source="rate_table")


def estimate(
    spec: StructuralSpec,
    prices: dict[Material, K.MaterialRecord],
    quote: ToolingQuote,
    ceiling_cny: float,
    *,
    price_source: str = "live",
) -> CostReport:
    densities = {m: r.density_g_cm3 for m, r in prices.items()}
    # tooling is paid off over this many units (10k/yr x 2 yrs)
    units = C.ANNUAL_VOLUME * C.TOOLING_AMORTIZATION_YEARS
    lines: list[CostLine] = []

    for p in spec.parts:
        mass_g = G.part_volume_cm3(p, spec) * densities[p.material]
        # resin cost, plus runner and scrap
        mat = mass_g / 1000.0 * prices[p.material].price_cny_per_kg * K.RUNNER_SCRAP_FACTOR
        lines.append(CostLine(item=f"material:{p.part_id}", cny=round(mat, 3)))
        # machine time per shot; thicker walls cool slower
        proc = K.cycle_time_s(p.nominal_wall_mm) / 3600.0 * K.MACHINE_RATE_CNY_PER_HOUR
        lines.append(CostLine(item=f"moulding:{p.part_id}", cny=round(proc, 3)))
        for op in p.secondary_ops:
            lines.append(CostLine(item=f"op:{p.part_id}:{op.value}", cny=K.SECONDARY_OP_CNY[op]))

    # one-off mould cost spread per unit
    lines.append(CostLine(item="tooling_amortised", cny=round(quote.total_cny / units, 3)))
    lines.append(CostLine(item="window_lens", cny=K.WINDOW_LENS_CNY))
    if spec.screw_count:
        lines.append(CostLine(item="screws", cny=round(spec.screw_count * K.SCREW_CNY, 3)))
    if spec.sealing != Sealing.NONE:
        lines.append(CostLine(item="gasket", cny=K.GASKET_CNY))
    if spec.sealing == Sealing.GASKET_MEMBRANE:
        lines.append(CostLine(item="membrane_vent", cny=K.MEMBRANE_VENT_CNY))

    # labour: base + per screw, more for gaskets and snap-fits
    assembly = K.ASSEMBLY_BASE_CNY + spec.screw_count * K.ASSEMBLY_PER_SCREW_CNY
    if spec.sealing != Sealing.NONE:
        assembly += K.ASSEMBLY_GASKET_CNY
    if spec.closure in (Closure.SNAP_FITS, Closure.HYBRID):
        assembly += K.ASSEMBLY_SNAP_CNY
    lines.append(CostLine(item="assembly", cny=round(assembly, 3)))

    # QA overhead as a % of everything above
    subtotal = sum(l.cny for l in lines)
    lines.append(CostLine(item="qa_overhead", cny=round(subtotal * K.QA_OVERHEAD, 3)))
    total = round(sum(l.cny for l in lines), 2)

    return CostReport(
        variant_id=spec.variant_id, revision=spec.revision, lines=lines, total_cny=total, ceiling_cny=ceiling_cny,
        within_budget=total <= ceiling_cny + 1e-9, tooling_total_cny=quote.total_cny,
        housing_mass_g=G.housing_mass_g(spec, densities),
        price_source="live" if price_source == "live" else "snapshot",
        quote_source="live" if quote.source == "live" else "rate_table",
    )
