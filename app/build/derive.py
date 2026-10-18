"""Derive a reference DimensionSpec from a known-good plan and its solid.

Used for benchmark ground truth and for drawing annotations. Each derived
dimension is checked on the solid it came from; any that doesn't hold (for
example a pocket edge-distance interrupted by a hole) is dropped and reported.
Hand-written extras must hold, or derivation fails loudly.
"""

from __future__ import annotations

import math

from app.build.builder import AXIS_INDEX, direction, hole_positions, in_plane_axes
from app.build.measure import Measurer
from app.build.schemas import (
    BuildPlan,
    DimensionSpec,
    HoleOp,
    PocketOp,
    RequiredDimension,
    StockBox,
)
from app.build.verify import MODEL_TOL_MM, check_dimension
from app.schemas import OpenQuestion, Vec3


class DerivationError(Exception):
    pass


def _v(t) -> Vec3:
    return Vec3(x=round(t[0], 4), y=round(t[1], 4), z=round(t[2], 4))


def _g(x: float) -> str:
    return f"{x:g}" if abs(x - round(x)) > 1e-9 else f"{int(round(x))}"


def derive_spec(plan: BuildPlan, shape, *, extras: list[RequiredDimension] = (), part_name: str | None = None,
                description: str = "", material_text: str | None = None, general_tolerance_mm: float | None = 0.1,
                ) -> tuple[DimensionSpec, list[str]]:
    m = Measurer(shape)
    auto: list[RequiredDimension] = []
    n = 0

    def add(**kw) -> None:
        nonlocal n
        n += 1
        tol = kw.pop("tol", general_tolerance_mm)
        auto.append(RequiredDimension(id=f"D{n}", tol_plus=tol, tol_minus=tol, **kw))

    for axis in ("x", "y", "z"):
        v = round(m.size[AXIS_INDEX[axis]], 3)
        add(label=_g(v), kind="overall", nominal=v, axis=axis)

    rel = m.to_rel
    if isinstance(plan.stock, StockBox) and plan.stock.corner_radius:
        r = plan.stock.corner_radius
        k = r * (1 - 1 / math.sqrt(2))
        add(label=f"R{_g(r)} 4X", kind="radius", nominal=r, axis="z", at=_v((k, k, m.size[2] / 2)))

    counts: dict[float, int] = {}
    for op in plan.operations:
        if isinstance(op, HoleOp):
            ua, va = in_plane_axes(op.direction)
            for p in hole_positions(op):
                p = rel(p)
                add(label=f"Ø{_g(op.diameter)}", kind="hole_diameter", nominal=op.diameter, at=_v(p))
                for ax in (ua, va):
                    add(label=_g(round(p[AXIS_INDEX[ax]], 3)), kind="hole_position", nominal=round(p[AXIS_INDEX[ax]], 4),
                        axis=ax, at=_v(p), feature_diameter=op.diameter)
                if op.depth is None:
                    add(label=f"Ø{_g(op.diameter)} THRU", kind="hole_depth", through=True, at=_v(p), feature_diameter=op.diameter)
                else:
                    add(label=f"Ø{_g(op.diameter)} x {_g(op.depth)} DP", kind="hole_depth", nominal=op.depth, at=_v(p), feature_diameter=op.diameter)
                if op.counterbore_diameter:
                    add(label=f"CBORE Ø{_g(op.counterbore_diameter)}", kind="hole_diameter", nominal=op.counterbore_diameter, at=_v(p))
                    add(label=f"CBORE Ø{_g(op.counterbore_diameter)} x {_g(op.counterbore_depth)} DP", kind="hole_depth",
                        nominal=op.counterbore_depth, at=_v(p), feature_diameter=op.counterbore_diameter)
                    counts[op.counterbore_diameter] = counts.get(op.counterbore_diameter, 0) + 1
                counts[op.diameter] = counts.get(op.diameter, 0) + 1

        elif isinstance(op, PocketOp) and not op.angle_deg:
            axis, s = direction(op.direction)
            ua, va = in_plane_axes(op.direction)
            ai, ui, vi = AXIS_INDEX[axis], AXIS_INDEX[ua], AXIS_INDEX[va]
            c = list(rel((op.center.x, op.center.y, op.center.z)))
            face = 0.0 if s > 0 else m.size[ai]
            if op.depth is None:
                mid = list(c)
                mid[ai] = m.size[ai] / 2
                add(label=f"{op.id} THRU", kind="depth", through=True, at=_v(c), direction=op.direction)
            else:
                floor = c[ai] + s * op.depth
                nominal = round(abs(face - floor), 4)
                # Probe the floor where no other feature pierces it: centre first,
                # then points between the centre and each corner.
                for fu, fv in ((0, 0), (0.25, 0.25), (-0.25, 0.25), (0.25, -0.25), (-0.25, -0.25), (0.4, 0), (-0.4, 0), (0, 0.4), (0, -0.4)):
                    pt = list(c)
                    pt[ui] += fu * op.length
                    pt[vi] += fv * op.width
                    cand = RequiredDimension(id="probe", label="", kind="depth", nominal=nominal, at=_v(pt), direction=op.direction)
                    if check_dimension(cand, m, MODEL_TOL_MM).passed:
                        break
                add(label=_g(round(abs(face - floor), 3)), kind="depth", nominal=nominal, at=_v(pt), direction=op.direction)
                mid = list(c)
                mid[ai] = c[ai] + s * op.depth / 2
            add(label=_g(op.length), kind="gap_width", nominal=op.length, axis=ua, at=_v(mid))
            add(label=_g(op.width), kind="gap_width", nominal=op.width, axis=va, at=_v(mid))
            for idx, size, ax in ((ui, op.length, ua), (vi, op.width, va)):
                wall = c[idx] - size / 2
                if wall > 0.5:
                    pt = list(mid)
                    pt[idx] = wall / 2
                    add(label=_g(round(wall, 3)), kind="material_width", nominal=round(wall, 4), axis=ax, at=_v(pt))
            r = op.corner_radius or 0.0
            half = min(op.length, op.width) / 2
            if 0 < r < half - 1e-9:
                k = r / math.sqrt(2)
                pt = list(mid)
                pt[ui] = c[ui] + op.length / 2 - r + k
                pt[vi] = c[vi] + op.width / 2 - r + k
                add(label=f"R{_g(r)} 4X", kind="radius", nominal=r, axis=axis, at=_v(pt))
            elif r > 0:
                pt = list(mid)
                long_i = ui if op.length >= op.width else vi
                pt[long_i] = c[long_i] + max(op.length, op.width) / 2
                add(label=f"R{_g(r)}", kind="radius", nominal=r, axis=axis, at=_v(pt))

    for diam, cnt in sorted(counts.items()):
        add(label=f"{cnt}X Ø{_g(diam)}", kind="hole_count", nominal=float(cnt), feature_diameter=diam, tol=None)

    dropped: list[str] = []
    kept: list[RequiredDimension] = []
    for d in auto:
        res = check_dimension(d, m, MODEL_TOL_MM)
        if res.passed:
            kept.append(d)
        else:
            dropped.append(f"{d.kind} {d.label} at {d.at}: {res.method}")

    final: list[RequiredDimension] = []
    for i, d in enumerate(kept + list(extras), start=1):
        final.append(d.model_copy(update={"id": f"D{i}"}))
    for d in final[len(kept):]:
        res = check_dimension(d, m, MODEL_TOL_MM)
        if not res.passed:
            raise DerivationError(f"extra dimension {d.label} ({d.kind}) does not hold on the reference model: "
                                  f"expected {res.expected}, measured {res.measured}. {res.method}")
    spec = DimensionSpec(part_name=part_name, units_on_input="mm", description=description, scale_basis="dimensioned",
                         general_tolerance_mm=general_tolerance_mm, material_text=material_text, dimensions=final,
                         open_questions=[])
    return spec, dropped


def no_scale_spec(description: str) -> DimensionSpec:
    """Ground truth for an input that carries no size information."""
    return DimensionSpec(
        part_name=None, units_on_input="unknown", description=description, scale_basis="none", dimensions=[],
        open_questions=[OpenQuestion(id="extract.no_scale", field="dimensions", ask="customer",
                                     question="The input shows no dimensions or reference object, so sizes can't be read. Ask for a dimensioned drawing or measurements.")],
    )
