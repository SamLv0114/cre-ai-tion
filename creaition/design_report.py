"""The run's final output in one readable file: `runs/<run_id>/report.md`, plus a sketch per variant.

The pipeline's product is a design package per variant (2D concept, 3D-ready structure, DFM / cost / quality
review). The JSON holds all of it but is hard to read, and the console log is about the process, not the result.
This writes one Markdown page per run: per brief, the outcome, then a design card for each shortlisted variant
with a front-view sketch drawn from the spec's own numbers. Plain code, no model calls; it can be rebuilt from a
saved run folder with `python -m creaition report <run folder>`.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any, Optional

from .domain.drift import concept_drift
from .domain.schemas import ConceptSpec, StructuralSpec

PX = 3.0  # sketch scale, pixels per mm

# rough colour for the sketch from the concept's colour words; light grey when nothing matches
_COLOURS = [("orange", "#f28c28"), ("yellow", "#f2d24b"), ("red", "#d9534f"), ("green", "#7fb77e"),
            ("blue", "#6f9fd8"), ("navy", "#3b4f7a"), ("black", "#3a3a3a"), ("charcoal", "#4a4a4a"),
            ("graphite", "#555555"), ("slate", "#6b7280"), ("grey", "#b8bcc2"), ("gray", "#b8bcc2"),
            ("sand", "#e3d5b8"), ("beige", "#e8dcc4"), ("white", "#f4f2ee")]


def _fill(colour: str) -> str:
    text = colour.lower()
    return next((hex_ for word, hex_ in _COLOURS if word in text), "#d9dce1")


def _face_size(env: dict, face: str) -> tuple[float, float]:
    """(horizontal, vertical) size of a face as drawn: front/back L x W, top/bottom L x H, left/right W x H."""
    if face in ("front", "back"):
        return env["length"], env["width"]
    if face in ("top", "bottom"):
        return env["length"], env["height"]
    return env["width"], env["height"]


def _outline(x: float, y: float, w: float, h: float, concept: dict, fill: str) -> str:
    """The housing's outline on the front view, shaped by the silhouette."""
    sil = concept["silhouette"]
    r = min(concept["corner_radius_mm"] * PX, w / 2, h / 2)
    if sil == "capsule":
        r = min(w, h) / 2
    if sil == "chamfered_slab":
        c = max(r, 4 * PX)
        pts = [(x + c, y), (x + w - c, y), (x + w, y + c), (x + w, y + h - c), (x + w - c, y + h), (x + c, y + h),
               (x, y + h - c), (x, y + c)]
        return (f'<polygon points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="{fill}" '
                f'stroke="#333" stroke-width="1.5"/>')
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r:.1f}" fill="{fill}" '
            f'stroke="#333" stroke-width="1.5"/>')


def _features(face: str, x: float, y: float, fw: float, fh: float, concept: dict, spec: dict) -> list[str]:
    """Display window, intake slots and USB opening that sit on this face, placed by the concept's percentages."""
    out = []

    def centre(placement: dict, w: float, h: float) -> tuple[float, float]:
        cx = x + placement["x_pct"] / 100 * fw
        cy = y + placement["y_pct"] / 100 * fh
        # keep the feature inside the face
        cx = min(max(cx, x + w / 2 + 2), x + fw - w / 2 - 2)
        cy = min(max(cy, y + h / 2 + 2), y + fh - h / 2 - 2)
        return cx, cy

    if concept["display"]["face"] == face:
        w, h = spec["display_window_mm"]["width"] * PX, spec["display_window_mm"]["height"] * PX
        w, h = min(w, fw - 6), min(h, fh - 6)
        cx, cy = centre(concept["display"], w, h)
        out.append(f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" rx="3" '
                   f'fill="#26323d" stroke="#111"/>')
    vent = spec["vent"]
    if vent["face"] == face:
        zw, zh = vent["zone_w_mm"] * PX, vent["zone_h_mm"] * PX
        # the zone's long side lies along the face's long side, as the DFM check assumes
        if (zw >= zh) != (fw >= fh):
            zw, zh = zh, zw
        zw, zh = min(zw, fw - 6), min(zh, fh - 6)
        cx, cy = centre(concept["intake"] if concept["intake"]["face"] == face else {"x_pct": 50, "y_pct": 75}, zw, zh)
        out.append(f'<rect x="{cx - zw / 2:.1f}" y="{cy - zh / 2:.1f}" width="{zw:.1f}" height="{zh:.1f}" '
                   f'fill="none" stroke="#555" stroke-dasharray="3,2"/>')
        n = max(1, int(vent["slot_count"]))
        long_side, short_side = (zw, zh) if zw >= zh else (zh, zw)
        slot_l = min(vent["slot_l_mm"] * PX, long_side - 4)
        slot_w = max(1.5, vent["slot_w_mm"] * PX)
        pitch = short_side / (n + 1)
        for i in range(n):
            off = -short_side / 2 + pitch * (i + 1)
            if zw >= zh:
                out.append(f'<rect x="{cx - slot_l / 2:.1f}" y="{cy + off - slot_w / 2:.1f}" width="{slot_l:.1f}" '
                           f'height="{slot_w:.1f}" rx="{slot_w / 2:.1f}" fill="#444"/>')
            else:
                out.append(f'<rect x="{cx + off - slot_w / 2:.1f}" y="{cy - slot_l / 2:.1f}" width="{slot_w:.1f}" '
                           f'height="{slot_l:.1f}" rx="{slot_w / 2:.1f}" fill="#444"/>')
    if concept["usb"]["face"] == face:
        w, h = spec["usb_opening_mm"]["width"] * PX, spec["usb_opening_mm"]["height"] * PX
        if (w >= h) != (fw >= fh):
            w, h = h, w
        cx, cy = centre(concept["usb"], w, h)
        out.append(f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{min(w, h) / 2:.1f}" '
                   f'fill="#111"/>')
    return out


def sketch_svg(concept: dict, spec: dict) -> str:
    """Front view plus every other face that carries a feature, drawn to scale from the final structure."""
    env = spec["envelope_mm"]
    fill = _fill(concept["cmf"]["color"])
    bumper = any(p["role"] == "bumper" or "tpe_overmold" in p["secondary_ops"] for p in spec["parts"])
    faces = ["front"] + [f for f in ("bottom", "top", "left", "right", "back")
                         if f in {concept["display"]["face"], concept["usb"]["face"], spec["vent"]["face"]}]
    if "bottom" not in faces:
        faces.insert(1, "bottom")  # always show one side view so the thickness is visible
    parts: list[str] = []
    x, y = 20.0, 34.0
    fw, fh = (s * PX for s in _face_size(env, "front"))
    # wide enough for the front view, and for all the other views side by side in one row
    side = sum(_face_size(env, f)[0] * PX for f in faces[1:]) + 30 * (len(faces) - 2)
    width = max(560.0, fw + 40, side + 40)
    parts.append(f'<text x="{x}" y="{y - 12}" class="t">FRONT  {env["length"]:g} x {env["width"]:g} mm</text>')
    if bumper:
        b = 2.5 * PX
        parts.append(f'<rect x="{x - b:.1f}" y="{y - b:.1f}" width="{fw + 2 * b:.1f}" height="{fh + 2 * b:.1f}" '
                     f'rx="{min(concept["corner_radius_mm"] * PX + b, (fh + 2 * b) / 2):.1f}" fill="#3d3d3d"/>')
    parts.append(_outline(x, y, fw, fh, concept, fill))
    parts += _features("front", x, y, fw, fh, concept, spec)
    # the other faces, side by side under the front view
    y2, x2, row_h = y + fh + 44, 20.0, 0.0
    for face in faces[1:]:
        sw, sh = (s * PX for s in _face_size(env, face))
        if x2 + sw > width - 10:
            x2, y2 = 20.0, y2 + row_h + 40
            row_h = 0.0
        parts.append(f'<text x="{x2}" y="{y2 - 10}" class="t">{face.upper()}  {sw / PX:g} x {sh / PX:g} mm</text>')
        parts.append(f'<rect x="{x2:.1f}" y="{y2:.1f}" width="{sw:.1f}" height="{sh:.1f}" rx="{min(6.0, sh / 2):.1f}" '
                     f'fill="{fill}" stroke="#333" stroke-width="1.5"/>')
        parts += _features(face, x2, y2, sw, sh, concept, spec)
        x2 += sw + 30
        row_h = max(row_h, sh)
    height = y2 + row_h + 40
    legend = ("dark panel: display window  ·  slots: air intake  ·  black pill: USB-C opening"
              + ("  ·  dark band: TPE bumper" if bumper else "") + "  ·  to scale, schematic (not CAD)")
    parts.append(f'<text x="20" y="{height - 14}" class="n">{escape(legend)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="0 0 {width:.0f} {height:.0f}">'
            '<style>.t{font:600 12px sans-serif;fill:#333}.n{font:11px sans-serif;fill:#666}</style>'
            f'<rect width="100%" height="100%" fill="#ffffff"/>{"".join(parts)}</svg>')


def _ops(ops: list[str]) -> str:
    return ", ".join(o.replace("_", " ") for o in ops) or "none"


def _card(rank: int, v: dict[str, Any]) -> list[str]:
    """Markdown design card for one variant."""
    r = v["result"]
    c, s, d, k, rv = r["concept"], r["structure"], r.get("dfm"), r.get("cost"), r.get("review")
    env = s["envelope_mm"]
    walls = sorted({p["nominal_wall_mm"] for p in s["parts"]})
    drafts = sorted({d_["draft_deg"] for p in s["parts"] for d_ in p["drafts"]})
    drift = concept_drift(ConceptSpec.model_validate(c), StructuralSpec.model_validate(s))
    lines = [f'### {rank}. {v["variant_id"]}: "{c["name"]}"', "",
             f'![{v["variant_id"]} sketch](sketches/{v["variant_id"]}.svg)', "",
             "| | |", "|---|---|",
             f'| **Status** | `{v["status"]}`: {r.get("reason") or ""} |',
             f'| **Form** | {c["form_language"]} (silhouette: {c["silhouette"].replace("_", " ")}, corner radius '
             f'{c["corner_radius_mm"]:g} mm) |',
             f'| **Size** | {env["length"]:g} x {env["width"]:g} x {env["height"]:g} mm |',
             f'| **CMF** | {c["cmf"]["material"]}, {c["cmf"]["finish"].replace("_", " ")}, {c["cmf"]["color"]}; '
             f'decoration: {_ops(c["cmf"]["secondary_ops"])} |',
             f'| **Layout** | display on {c["display"]["face"]} ({s["display_window_mm"]["width"]:g} x '
             f'{s["display_window_mm"]["height"]:g} mm), intake on {s["vent"]["face"]} ({s["vent"]["slot_count"]} slots), '
             f'USB-C on {c["usb"]["face"]} |',
             f'| **Structure** | {len(s["parts"])} parts ({", ".join(p["part_id"] for p in s["parts"])}); wall '
             f'{"/".join(f"{w:g}" for w in walls)} mm; draft {"/".join(f"{x:g}" for x in drafts)} deg; '
             f'{s["closure"].replace("_", " ")} ({s["screw_count"]} screws); sealing {s["sealing"].replace("_", " ")} |']
    if d:
        fails = sorted({x["rule_id"] for x in d["results"] if x["status"] == "fail"})
        lines.append(f'| **DFM** | {"pass" if not fails else "FAIL: " + ", ".join(fails)}; mass {d["product_mass_g"]:.0f} g; '
                     f'vent open area {d["open_area_ratio"]:.0%}. First draft failed '
                     f'{", ".join(r.get("first_dfm_failures") or []) or "nothing"}; fixed with {r.get("autofixes", 0)} '
                     f'autofix round(s) and {r.get("llm_revisions", 0)} LLM revision(s) |')
    if k:
        lines.append(f'| **Cost** | CNY {k["total_cny"]:.2f} per housing (ceiling {k["ceiling_cny"]:g}); tooling '
                     f'CNY {k["tooling_total_cny"]:,.0f} |')
    if rv:
        sc = rv["scores"]
        lines.append(f'| **Judge** | {rv["verdict"]}: brief {sc["brief_adherence"]}, form {sc["form_coherence"]}, '
                     f'CMF {sc["cmf_fit"]}, usability {sc["usability"]}, intent {sc["intent_preservation"]} (of 5) |')
    lines.append(f'| **Changed from the concept** | {"; ".join(drift) if drift else "nothing (checked by code)"} |')
    lines += ["", "Design intent and where the 3D spec keeps it:", ""]
    trace = {t["anchor_id"]: t["realized_by"] for t in s["intent_trace"]}
    lines += [f'- **{a["anchor_id"]}** {a["statement"]} → `{", ".join(trace.get(a["anchor_id"], ["(not traced)"]))}`'
              for a in c["intent_anchors"]]
    if r.get("caveats"):
        lines += ["", "Caveats for the reviewer:", ""] + [f"- {x}" for x in r["caveats"]]
    if rv:
        note = rv["rationale"] if len(rv["rationale"]) <= 400 else rv["rationale"][:400].rsplit(" ", 1)[0] + " ..."
        lines += ["", f"> Judge: {note}"]
    lines += ["", f'Full detail: `python -m creaition show {v["result"]["run_id"]} {v["variant_id"]}` or '
                  f'`variants/{v["variant_id"]}.json`.', ""]
    return lines


def build_report(run_dir: Path, summary: dict[str, Any], variants: list[dict[str, Any]],
                 briefs: Optional[dict[str, dict[str, Any]]] = None) -> Path:
    """Write report.md and sketches/*.svg into run_dir. `briefs` maps brief id -> {title, text} when known."""
    briefs = briefs or {}
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    by_id = {v["variant_id"]: v for v in variants}
    counts = summary.get("status_counts", {})
    L = [f'# Design report: {summary["run_id"]}', "",
         f'Config `{summary["config"]}`, mode **{summary["llm_mode"]}**, {summary["variants"]} variants: '
         + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
         + f'. Model cost CNY {summary.get("model_cost_cny_total", 0):.2f}.', "",
         "Each brief below shows its outcome, then one design card per shortlisted variant: the 2D concept, the "
         "3D-ready structure and the DFM / cost / quality review, with a sketch drawn from the spec's numbers. "
         "Status `pending_approval` means every automated check passed and a person still has to sign off; "
         "`needs_human` means it passed with caveats a person must look at.", ""]
    for bid, b in summary["briefs"].items():
        info = briefs.get(bid, {})
        L += [f'## {bid}: {info.get("title") or b.get("title") or ""}', ""]
        if info.get("text"):
            L += [f'> {info["text"]}', ""]
        facts = [f'cost ceiling CNY {b["cost_ceiling_cny"]:g}' if b.get("cost_ceiling_cny") is not None else None,
                 f'cheapest possible CNY {b["floor_cost_cny"]:.2f}' if b.get("floor_cost_cny") is not None else None,
                 "conflict found" if b.get("conflict_detected") else None,
                 "needs a person to resolve the brief" if b.get("requires_human_resolution") else None]
        L += ["**Outcome:** " + "; ".join(f for f in facts if f) + ".", ""]
        if b.get("infeasible"):
            L += ["**Not feasible as written, so no variants were generated:**", ""]
            L += [f"- {x}" for x in b["infeasible_reasons"]] + ["", f"See `infeasible_{bid}.md`.", ""]
            continue
        if b.get("quality_paused"):
            L += [f'**Paused by the quality breaker:** {b["quality_paused"]}', ""]
        if any(a.startswith("Relaxed") for a in b.get("assumptions") or []):
            L += [f'This brief can only be made after relaxing the limits below, so it got {b.get("variants_planned", 2)} '
                  "example variants instead of the full fan-out, and none of them can be auto-approved.", ""]
        for a in b.get("assumptions") or []:
            L.append(f"- assumption: {a}")
        if b.get("assumptions"):
            L.append("")
        shortlist = [by_id[x] for x in b.get("shortlist", []) if x in by_id]
        if shortlist:
            L += ["Shortlist: " + ", ".join(v["variant_id"] for v in shortlist), ""]
        for i, v in enumerate(shortlist, 1):
            (run_dir / "sketches").mkdir(exist_ok=True)
            (run_dir / "sketches" / f'{v["variant_id"]}.svg').write_text(
                sketch_svg(v["result"]["concept"], v["result"]["structure"]))
            L += _card(i, v)
        others = [v for v in variants if v["brief_id"] == bid and v["variant_id"] not in b.get("shortlist", [])]
        if others:
            L += ["Not shortlisted:", "", "| variant | status | reason |", "|---|---|---|"]
            L += [f'| {v["variant_id"]} | `{v["status"]}` | {((v.get("result") or {}).get("reason") or "").replace("|", "/")} |'
                  for v in others]
            L.append("")
    path = run_dir / "report.md"
    path.write_text("\n".join(L) + "\n")
    return path


def build_from_folder(run_dir: Path, briefs: Optional[dict[str, dict[str, Any]]] = None) -> Path:
    """Rebuild report.md from a saved run folder (summary.json + variants/*.json), e.g. a live eval run."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    variants = [json.loads(p.read_text()) for p in sorted((run_dir / "variants").glob("*.json"))]
    return build_report(run_dir, summary, variants, briefs)
