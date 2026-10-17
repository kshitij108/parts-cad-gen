"""DFM stage: deterministic rule checks for 3-axis CNC milling.

Each flag carries the measured value, the threshold it was compared against,
where both came from, and a template-built reason a machinist can verify. An
LLM may later reword `explanation`, but it never adds or changes numbers.
Checks that can't run are listed in `checks_skipped` with the reason, so a
clean report is never mistaken for a clean part.
"""

from __future__ import annotations

from collections import Counter

from app.schemas import DfmFlag, DfmInput, DfmReport, Evidence, GeometrySummary, Vec3

ParallelTol = 0.999


def tool_axis(geometry: GeometrySummary) -> tuple[Vec3, str]:
    """Primary tool axis assumption: the most common hole axis, else file Z."""
    if geometry.hole_candidates:
        axes = Counter((h.axis.x, h.axis.y, h.axis.z) for h in geometry.hole_candidates)
        (x, y, z), n = axes.most_common(1)[0]
        return Vec3(x=x, y=y, z=z), f"most common hole axis ({n} of {len(geometry.hole_candidates)} holes)"
    return Vec3(x=0, y=0, z=1), "file Z axis (no holes to infer orientation from)"


def _parallel(a: Vec3, b: Vec3) -> bool:
    return abs(a.x * b.x + a.y * b.y + a.z * b.z) >= ParallelTol


def tightest_tolerance(inp_drawing, spec) -> tuple[float | None, Evidence | None]:
    candidates: list[tuple[float, Evidence]] = []
    for t in inp_drawing.tolerances:
        candidates.append((t.band_mm, Evidence(source="drawing", ref=f"drawing.tolerance[{t.text}]", detail=f"{t.band_mm} mm band (confidence {t.confidence:.0%})")))
    if spec.tightest_tolerance_mm is not None:
        candidates.append((spec.tightest_tolerance_mm, Evidence(source="intake", ref="spec.tightest_tolerance_mm", detail=f"{spec.tightest_tolerance_mm} mm band, entered by shop")))
    if not candidates:
        return None, None
    return min(candidates, key=lambda c: c[0])


def run(inp: DfmInput) -> DfmReport:
    g, rc, spec = inp.geometry, inp.rate_card, inp.spec
    flags: list[DfmFlag] = []
    run_: list[str] = []
    skipped: dict[str, str] = {
        "deep_pockets": "Pocket recognition is not implemented in Phase 1.",
        "undercuts": "Undercut detection is not implemented in Phase 1.",
        "thread_callouts": "Threads are read from the drawing; drawing extraction is not live yet.",
    }
    material = rc.material(spec.material_id)

    # --- Fits a machine --------------------------------------------------
    run_.append("machine_envelope")
    part = sorted([g.bbox_mm.x, g.bbox_mm.y, g.bbox_mm.z])
    fits = [m for m in rc.machines if all(p <= e for p, e in zip(part, sorted([m.envelope_mm.x, m.envelope_mm.y, m.envelope_mm.z])))]
    if not fits:
        flags.append(DfmFlag(
            id="envelope", rule="machine_envelope", severity="critical",
            title="Part does not fit any machine on the rate card",
            measured=f"{g.bbox_mm.x:.1f} × {g.bbox_mm.y:.1f} × {g.bbox_mm.z:.1f} mm",
            threshold="; ".join(f"{m.name}: {m.envelope_mm.x:.0f}×{m.envelope_mm.y:.0f}×{m.envelope_mm.z:.0f}" for m in rc.machines),
            reason="Bounding box (any orientation) exceeds every machine's travel.",
            suggested_fix="Outsource, split the part, or add a larger machine to the rate card.",
            evidence=[Evidence(source="geometry", ref="geometry.bbox_mm", detail=f"{part}")],
        ))

    # --- Thin walls ------------------------------------------------------
    wt = g.wall_thickness
    if material is None:
        skipped["thin_walls"] = "Material not set; minimum wall depends on material."
    elif wt.min_mm is None:
        skipped["thin_walls"] = "Wall sampling produced no readings."
    else:
        run_.append("thin_walls")
        if wt.min_mm < material.min_wall_mm:
            sev = "critical" if wt.min_mm < material.min_wall_mm / 2 else "warn"
            flags.append(DfmFlag(
                id="thin_wall", rule="thin_walls", severity=sev,
                title="Wall thinner than the shop minimum for this material",
                measured=f"{wt.min_mm:.2f} mm (estimate, {wt.samples} samples)",
                threshold=f"{material.min_wall_mm:.2f} mm for {material.name}",
                reason=f"Walls under {material.min_wall_mm:.2f} mm in {material.name} chatter and deflect during cutting, so size and finish can't be held.",
                suggested_fix=f"Thicken to ≥ {material.min_wall_mm:.2f} mm, or plan light finishing passes and expect a higher price.",
                evidence=[
                    Evidence(source="geometry", ref="geometry.wall_thickness.min_mm", detail=f"{wt.min_mm} mm"),
                    Evidence(source="rate_card", ref=f"rate_card.materials[{material.id}].min_wall_mm", detail=f"{material.min_wall_mm} mm"),
                ],
            ))

    # --- Holes -----------------------------------------------------------
    run_.append("hole_depth_ratio")
    run_.append("small_holes")
    t = rc.dfm
    for i, h in enumerate(g.hole_candidates):
        ratio = h.depth_mm / h.diameter_mm if h.diameter_mm else 0
        ev = [Evidence(source="geometry", ref=f"geometry.hole_candidates[{i}]", detail=f"Ø{h.diameter_mm:.2f} × {h.depth_mm:.2f} mm")]
        if ratio > t.hole_depth_ratio_warn:
            sev = "critical" if ratio > t.hole_depth_ratio_critical else "warn"
            flags.append(DfmFlag(
                id=f"deep_hole.{i}", rule="hole_depth_ratio", severity=sev,
                title=f"Deep hole Ø{h.diameter_mm:.2f} mm",
                measured=f"depth/diameter {ratio:.1f}:1",
                threshold=f"warn > {t.hole_depth_ratio_warn:.0f}:1, critical > {t.hole_depth_ratio_critical:.0f}:1",
                reason="Deep holes need peck drilling or gun drilling; chip evacuation and drill wander get worse with depth.",
                suggested_fix="Reduce depth, increase diameter, or drill from both sides if it is a through hole.",
                evidence=ev + [Evidence(source="rate_card", ref="rate_card.dfm.hole_depth_ratio_warn", detail=f"{t.hole_depth_ratio_warn}")],
            ))
        if h.diameter_mm < t.min_tool_diameter_mm:
            flags.append(DfmFlag(
                id=f"small_hole.{i}", rule="small_holes", severity="warn",
                title=f"Hole smaller than the smallest tool",
                measured=f"Ø{h.diameter_mm:.2f} mm",
                threshold=f"Ø{t.min_tool_diameter_mm:.2f} mm",
                reason="The shop's smallest tool is larger than this hole.",
                suggested_fix="Enlarge the hole, or quote EDM / micro-drilling as an outside process.",
                evidence=ev + [Evidence(source="rate_card", ref="rate_card.dfm.min_tool_diameter_mm", detail=f"{t.min_tool_diameter_mm} mm")],
            ))

    # --- Internal corners ------------------------------------------------
    run_.append("internal_corners")
    axis, axis_why = tool_axis(g)
    min_r = t.min_tool_diameter_mm / 2
    small = [r for r in g.concave_radii if r.radius_mm < min_r]
    if small:
        flags.append(DfmFlag(
            id="small_internal_radius", rule="internal_corners", severity="warn",
            title=f"{len(small)} internal radii smaller than the smallest tool radius",
            measured=f"min R{min(r.radius_mm for r in small):.2f} mm",
            threshold=f"R{min_r:.2f} mm (half of Ø{t.min_tool_diameter_mm:.2f} tool)",
            reason="An end mill can't cut an internal radius smaller than its own radius.",
            suggested_fix=f"Increase internal radii to ≥ R{min_r:.2f} mm (larger is faster and cheaper).",
            evidence=[Evidence(source="geometry", ref="geometry.concave_radii", detail=f"{len(small)} faces")],
        ))
    vertical = [e for e in g.sharp_internal_edges if _parallel(e.direction, axis)]
    if vertical:
        flags.append(DfmFlag(
            id="sharp_internal_corner", rule="internal_corners", severity="critical",
            title=f"{len(vertical)} sharp internal corners along the tool axis",
            measured=f"{len(vertical)} edges with zero radius, longest {max(e.length_mm for e in vertical):.1f} mm",
            threshold="corner radius ≥ tool radius",
            reason=f"A rotating end mill leaves a radius in corners parallel to its axis, so these can't be cut sharp. Tool axis assumed: {axis_why}.",
            suggested_fix=f"Add a corner radius (≥ R{min_r:.2f} mm; ideally ≥ 1/3 of pocket depth), use dog-bone reliefs, or quote wire EDM.",
            evidence=[Evidence(source="geometry", ref="geometry.sharp_internal_edges", detail=f"{len(vertical)} of {len(g.sharp_internal_edges)} edges parallel to ({axis.x:g}, {axis.y:g}, {axis.z:g})")],
        ))

    # --- Tolerances ------------------------------------------------------
    band, ev = tightest_tolerance(inp.drawing, spec)
    if band is None:
        skipped["tight_tolerances"] = "No tolerance read from a drawing or entered by the shop."
    elif rc.machines:
        run_.append("tight_tolerances")
        best = min(m.best_tolerance_mm for m in rc.machines)
        std = min(m.standard_tolerance_mm for m in rc.machines)
        if band < std:
            sev = "critical" if band < best else "info"
            flags.append(DfmFlag(
                id="tight_tolerance", rule="tight_tolerances", severity=sev,
                title="Tolerance tighter than standard capability" + (" and beyond best capability" if band < best else ""),
                measured=f"{band:.3f} mm band",
                threshold=f"standard {std:.3f} mm, best {best:.3f} mm",
                reason="Tighter than standard means slower finishing passes and more inspection; beyond best capability the shop can't commit to it.",
                suggested_fix="Ask whether the tolerance is functional; loosen it where it isn't, or quote grinding/outside inspection.",
                evidence=[ev, Evidence(source="rate_card", ref="rate_card.machines[*].standard_tolerance_mm", detail=f"{std} mm")],
            ))

    rank = {"critical": 0, "warn": 1, "info": 2}
    flags.sort(key=lambda f: rank[f.severity])
    return DfmReport(flags=flags, checks_run=run_, checks_skipped=skipped)
