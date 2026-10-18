"""Deterministic plan edits for accepted DFM fixes.

A fix changes geometry, so it also adds the new value to the confirmed spec
(source "dfm_fix"). The edited plan is then rebuilt and re-verified like any
other attempt, and a person must accept it again.
"""

from __future__ import annotations

import math

from app.build.builder import direction, in_plane_axes
from app.build.schemas import (
    BuildPlan,
    CutBoxOp,
    DimensionSpec,
    PocketOp,
    RequiredDimension,
    StockExtrusion,
)
from app.schemas import DfmFlag, RateCard, Vec3

FIXABLE_RULES = {"internal_corners"}


def _round_up_half(x: float) -> float:
    return math.ceil(x * 2) / 2


def _next_id(spec: DimensionSpec) -> str:
    nums = [int(d.id[1:]) for d in spec.dimensions if d.id[1:].isdigit()]
    return f"D{max(nums, default=0) + 1}"


def fix_internal_corners(plan: BuildPlan, spec: DimensionSpec, rate_card: RateCard,
                         tool_axis: str = "z") -> tuple[BuildPlan, DimensionSpec, list[str]]:
    """Give sharp internal corners parallel to the tool axis a radius the shop can cut.

    Radius = max(smallest tool radius, depth / 3 rounded up to 0.5 mm), capped
    so it still fits the feature.
    """
    r_min = rate_card.dfm.min_tool_diameter_mm / 2
    plan = plan.model_copy(deep=True)
    spec = spec.model_copy(deep=True)
    changes: list[str] = []

    for op in plan.operations:
        if isinstance(op, PocketOp) and direction(op.direction)[0] == tool_axis and not op.angle_deg:
            half = min(op.length, op.width) / 2
            if (op.corner_radius or 0) >= r_min or half <= r_min:
                continue
            depth = op.depth if op.depth is not None else 0.0
            r = min(max(r_min, _round_up_half(depth / 3)), half - 0.01)
            before = op.corner_radius
            op.corner_radius = r
            axis, s = direction(op.direction)
            ua, va = in_plane_axes(op.direction)
            c = {"x": op.center.x, "y": op.center.y, "z": op.center.z}
            k = r / math.sqrt(2)
            c[ua] += op.length / 2 - r + k
            c[va] += op.width / 2 - r + k
            c[axis] += s * (depth / 2 if op.depth is not None else 0.5)
            new_id = _next_id(spec)
            spec.dimensions.append(RequiredDimension(
                id=new_id, label=f"R{r:g} 4X (DFM fix)", kind="radius", nominal=r, axis=axis,
                at=Vec3(**{a: round(v, 4) for a, v in c.items()}), source="dfm_fix", confidence=1.0,
                note=f"Added by DFM fix on {op.id}: internal corners need a radius ≥ R{r_min:g}."))
            op.dims.append(new_id)
            changes.append(f"{op.id}: corner radius set to R{r:g} (was {f'R{before:g}' if before else 'sharp'}).")

        elif isinstance(op, CutBoxOp) and (op.corner_radius or 0) < r_min:
            op.corner_radius = r_min
            op.corner_axis = tool_axis  # type: ignore[assignment]
            changes.append(f"{op.id}: inside corners parallel to {tool_axis.upper()} rounded to R{r_min:g}.")

    st = plan.stock
    if isinstance(st, StockExtrusion) and {"xy": "z", "xz": "y", "yz": "x"}[st.plane] == tool_axis:
        if (st.concave_corner_radius or 0) < r_min:
            st.concave_corner_radius = r_min
            changes.append(f"Outline inside corners rounded to R{r_min:g}.")

    if changes:
        plan.assumptions.append("DFM fix applied: " + " ".join(changes))
    return plan, spec, changes


def apply_fix(flag: DfmFlag, plan: BuildPlan, spec: DimensionSpec, rate_card: RateCard,
              tool_axis: str = "z") -> tuple[BuildPlan, DimensionSpec, list[str]]:
    if flag.rule == "internal_corners":
        return fix_internal_corners(plan, spec, rate_card, tool_axis)
    return plan, spec, []
