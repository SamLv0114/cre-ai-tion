"""`python -m creaition show`: print a variant's result in readable form, or list a run's variants.

A variant's JSON holds everything (concept, structure, DFM, cost, review, history) but is long. This prints the
parts a designer or reviewer looks at first. Works on a run in the workspace or on a saved variant JSON file.
"""

from __future__ import annotations

from typing import Any

from .domain.drift import concept_drift
from .domain.schemas import ConceptSpec, StructuralSpec


def _box(d: dict) -> str:
    return f'{d["length"]:g} x {d["width"]:g} x {d["height"]:g} mm'


def list_variants(variants: list[dict[str, Any]]) -> str:
    """One line per variant: status, id, reason."""
    lines = []
    for v in variants:
        r = v.get("result") or {}
        lines.append(f'{v["status"]:<17} {v["variant_id"]:<10} {(r.get("reason") or "")[:90]}')
    return "\n".join(lines)


def render_variant(v: dict[str, Any]) -> str:
    """Readable summary of one variant record ({variant_id, status, result})."""
    r = v.get("result") or {}
    out = [f'== {v["variant_id"]}  [{v["status"]}]  {r.get("reason") or ""}']
    for c in r.get("caveats") or []:
        out.append(f"   caveat: {c}")
    c = r.get("concept")
    if c:
        cmf = c["cmf"]
        out += ["", f'-- 2D concept: "{c["name"]}"  (direction: {c["direction"]})',
                f'form: {c["form_language"]}',
                f'silhouette {c["silhouette"]} | {_box(c["envelope_mm"])} | corner radius {c["corner_radius_mm"]:g} mm',
                f'CMF: {cmf["material"]}, {cmf["finish"]}, "{cmf["color"]}", secondary ops {cmf["secondary_ops"]}',
                f'display on {c["display"]["face"]} ({c["display_window_mm"]["width"]:g} x '
                f'{c["display_window_mm"]["height"]:g} mm) | intake on {c["intake"]["face"]} | USB on {c["usb"]["face"]}',
                "intent anchors:"]
        out += [f'  {a["anchor_id"]}: {a["statement"]}' for a in c["intent_anchors"]]
    s = r.get("structure")
    if s:
        out += ["", f'-- 3D structure (revision {s["revision"]}): {_box(s["envelope_mm"])}, parting {s["parting"]}']
        for p in s["parts"]:
            drafts = ", ".join(f'{d["face"]} {d["draft_deg"]:g} deg' for d in p["drafts"])
            out.append(f'  {p["part_id"]}: {p["material"]}, {p["finish"]}, wall {p["nominal_wall_mm"]:g} mm '
                       f'({p["min_wall_mm"]:g}-{p["max_wall_mm"]:g}); ribs {len(p["ribs"])}, bosses {len(p["bosses"])}, '
                       f'undercuts {len(p["undercuts"])}; ops {p["secondary_ops"]}; draft: {drafts}')
        vt = s["vent"]
        out.append(f'  vent on {vt["face"]}: {vt["zone_w_mm"]:g} x {vt["zone_h_mm"]:g} mm, {vt["slot_count"]} slots '
                   f'{vt["slot_w_mm"]:g} x {vt["slot_l_mm"]:g} mm | USB opening {s["usb_opening_mm"]["width"]:g} x '
                   f'{s["usb_opening_mm"]["height"]:g} mm | closure {s["closure"]} ({s["screw_count"]} screws) | '
                   f'sealing {s["sealing"]}')
        out.append("  intent trace: " + "; ".join(f'{t["anchor_id"]} -> {", ".join(t["realized_by"])}'
                                                for t in s["intent_trace"]))
        out.append("  change log:")
        out += [f"   - {x}" for x in s["change_log"]]
        if c:
            drift = concept_drift(ConceptSpec.model_validate(c), StructuralSpec.model_validate(s))
            out.append(f'  changed from the concept (code check): {"; ".join(drift) if drift else "nothing"}')
    d = r.get("dfm")
    if d:
        fails = sorted({x["rule_id"] for x in d["results"] if x["status"] == "fail"})
        out += ["", f'-- DFM: {"pass" if not fails else "FAIL " + str(fails)} | margin {d["headroom"]} | '
                    f'mass {d["product_mass_g"]} g | vent open area {d["open_area_ratio"]:.0%} | '
                    f'USB clearance {d["usb_clearance_mm"]} mm',
                f'   first draft failed: {r.get("first_dfm_failures")} | autofix rounds {r.get("autofixes")}, '
                f'LLM revisions {r.get("llm_revisions")}, fallback used: {r.get("fallback_used")}']
    k = r.get("cost")
    if k:
        out += [f'-- Cost: CNY {k["total_cny"]:.2f} (ceiling {k["ceiling_cny"]:g}), tooling CNY {k["tooling_total_cny"]:,.0f}, '
                f'prices {k["price_source"]}, quote {k["quote_source"]}']
        out += [f'   {line["item"]:<36} {line["cny"]:>7.2f}' for line in k["lines"]]
    rv = r.get("review")
    if rv:
        out += [f'-- Judge ({rv["judge_model"]}): {rv["verdict"]} | '
                + ", ".join(f"{name} {score}" for name, score in rv["scores"].items()),
                f'   {rv["rationale"]}']
    u = r.get("usage")
    if u:
        out.append(f'-- Usage: {u["prompt_tokens"] + u["completion_tokens"]} tokens, CNY {u["cost_cny"]:.3f}, '
                   f'{u["calls"]} model calls, models {sorted(set(r.get("models") or []))}')
    return "\n".join(out)
