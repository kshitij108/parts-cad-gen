"""Flat wire format the planner answers in, converted to BuildPlan.

Structured outputs cap a schema at 16 union-typed fields (each `X or null`
counts) and 24 optional ones, and unions compile exponentially. BuildPlan's
discriminated unions and nullable fields exceed that, so the planner instead
fills a flat format where every field is required and unused fields carry an
explicit "not used" value: 0 for numbers, [] for lists, "none" for choices.
Conversion validates everything and raises PlanInvalid with a readable reason,
which goes back to the planner as feedback.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.build.schemas import BuildPlan


class PlanInvalid(Exception):
    """The model answered, but the plan breaks a rule the schema can't express. Retried with feedback."""


class WireStock(BaseModel):
    kind: Literal["box", "cylinder", "extrusion"]
    size_x: float = Field(description="box: length along X. 0 if not a box.")
    size_y: float = Field(description="box: width along Y. 0 if not a box.")
    size_z: float = Field(description="box: height along Z. 0 if not a box.")
    corner_radius: float = Field(description="box: radius on the four vertical edges. 0 = sharp.")
    diameter: float = Field(description="cylinder: diameter (axis along Z at (d/2, d/2)). 0 if not a cylinder.")
    height: float = Field(description="cylinder: height along Z. 0 if not a cylinder.")
    plane: Literal["xy", "xz", "yz", "none"] = Field(description="extrusion: sketch plane (xy extrudes +Z, xz extrudes +Y, yz extrudes +X). none if not an extrusion.")
    points: list[list[float]] = Field(description="extrusion: closed outline as [[u, v], ...] in the sketch plane, first point not repeated. [] if not an extrusion.")
    depth: float = Field(description="extrusion: extrusion length. 0 if not an extrusion.")
    concave_corner_radius: float = Field(description="extrusion: radius on inside outline corners. 0 = sharp.")
    convex_corner_radius: float = Field(description="extrusion: radius on outside outline corners. 0 = sharp.")


class WireOp(BaseModel):
    op: Literal["hole", "pocket", "boss", "cut_box"]
    id: str = Field(description="Short unique id, e.g. H1, P1.")
    dims: list[str] = Field(description="Ids of the confirmed dimensions this operation implements.")
    direction: Literal["+x", "-x", "+y", "-y", "+z", "-z", "none"] = Field(
        description="hole/pocket: cutting direction into the material (-z = from the top down). boss: direction it grows. none for cut_box.")
    positions: list[list[float]] = Field(description="hole: entry points [[x, y, z], ...] on the face drilled from. [] when using bolt_circle or not a hole.")
    bolt_circle: list[float] = Field(description="hole: [centre_x, centre_y, centre_z, pitch_diameter, count, start_angle_deg], or [] if not used.")
    diameter: float = Field(description="hole diameter, or cylinder boss diameter. 0 otherwise.")
    depth: float = Field(description="hole/pocket depth from the entry face. 0 = through all.")
    counterbore_diameter: float = Field(description="hole: counterbore diameter. 0 = no counterbore.")
    counterbore_depth: float = Field(description="hole: counterbore depth. 0 = no counterbore.")
    center: list[float] = Field(description="pocket: centre of the opening on the entry face; boss: centre of its footprint. [x, y, z], or [] otherwise.")
    length: float = Field(description="pocket / box boss: size along the first in-plane axis (x for ±z, y for ±x, x for ±y). 0 otherwise.")
    width: float = Field(description="pocket / box boss: size along the second in-plane axis (y for ±z, z for ±x and ±y). 0 otherwise.")
    corner_radius: float = Field(description="pocket: radius of corners parallel to the direction (width/2 = obround slot); cut_box: radius on edges parallel to corner_axis. 0 = sharp.")
    angle_deg: float = Field(description="pocket: rotation about the cutting axis. 0 = none.")
    shape: Literal["cylinder", "box", "none"] = Field(description="boss shape. none for other operations.")
    height: float = Field(description="boss height. 0 otherwise.")
    corner_min: list[float] = Field(description="cut_box: minimum corner [x, y, z] of the region removed. [] otherwise.")
    corner_max: list[float] = Field(description="cut_box: maximum corner [x, y, z]. [] otherwise.")
    corner_axis: Literal["x", "y", "z", "none"] = Field(description="cut_box: axis of the edges corner_radius rounds. none otherwise.")


class WirePlan(BaseModel):
    stock: WireStock
    operations: list[WireOp]
    assumptions: list[str] = Field(description="Every value or choice not taken from the confirmed dimensions, with the reason.")


def _xyz(v: list[float], what: str) -> dict:
    if len(v) != 3:
        raise PlanInvalid(f"{what} must be [x, y, z] (got {v}).")
    return {"x": v[0], "y": v[1], "z": v[2]}


def _opt(v: float) -> float | None:
    return v if v else None


def to_plan(w: WirePlan) -> BuildPlan:
    s = w.stock
    if s.kind == "box":
        stock = {"kind": "box", "size": {"x": s.size_x, "y": s.size_y, "z": s.size_z}, "corner_radius": _opt(s.corner_radius)}
    elif s.kind == "cylinder":
        stock = {"kind": "cylinder", "diameter": s.diameter, "height": s.height}
    else:
        if s.plane == "none":
            raise PlanInvalid("extrusion stock needs a plane (xy, xz or yz).")
        bad = [p for p in s.points if len(p) != 2]
        if bad:
            raise PlanInvalid(f"extrusion points must be [u, v] pairs (got {bad[0]}).")
        stock = {"kind": "extrusion", "plane": s.plane, "points": [{"u": p[0], "v": p[1]} for p in s.points],
                 "depth": s.depth, "concave_corner_radius": _opt(s.concave_corner_radius),
                 "convex_corner_radius": _opt(s.convex_corner_radius)}

    ops = []
    for o in w.operations:
        base = {"op": o.op, "id": o.id, "dims": o.dims}
        if o.op in ("hole", "pocket", "boss") and o.direction == "none":
            raise PlanInvalid(f"{o.id}: {o.op} needs a direction.")
        if o.op == "hole":
            bc = None
            if o.bolt_circle:
                if len(o.bolt_circle) != 6 or o.bolt_circle[4] != int(o.bolt_circle[4]):
                    raise PlanInvalid(f"{o.id}: bolt_circle must be [cx, cy, cz, pitch_diameter, count, start_angle_deg].")
                cx, cy, cz, pcd, n, start = o.bolt_circle
                bc = {"center": {"x": cx, "y": cy, "z": cz}, "diameter": pcd, "count": int(n), "start_angle_deg": start}
            ops.append({**base, "diameter": o.diameter, "direction": o.direction,
                        "positions": [_xyz(p, f"{o.id} position") for p in o.positions], "bolt_circle": bc,
                        "depth": _opt(o.depth), "counterbore_diameter": _opt(o.counterbore_diameter),
                        "counterbore_depth": _opt(o.counterbore_depth)})
        elif o.op == "pocket":
            ops.append({**base, "center": _xyz(o.center, f"{o.id} center"), "direction": o.direction, "length": o.length,
                        "width": o.width, "depth": _opt(o.depth), "corner_radius": _opt(o.corner_radius),
                        "angle_deg": _opt(o.angle_deg)})
        elif o.op == "boss":
            if o.shape == "none":
                raise PlanInvalid(f"{o.id}: boss needs a shape (cylinder or box).")
            ops.append({**base, "shape": o.shape, "base_center": _xyz(o.center, f"{o.id} center"), "direction": o.direction,
                        "height": o.height, "diameter": _opt(o.diameter), "length": _opt(o.length), "width": _opt(o.width)})
        else:
            ops.append({**base, "corner_min": _xyz(o.corner_min, f"{o.id} corner_min"),
                        "corner_max": _xyz(o.corner_max, f"{o.id} corner_max"), "corner_radius": _opt(o.corner_radius),
                        "corner_axis": None if o.corner_axis == "none" else o.corner_axis})
    try:
        return BuildPlan.model_validate({"stock": stock, "operations": ops, "assumptions": w.assumptions})
    except ValidationError as e:
        raise PlanInvalid(f"Plan did not validate: {e}") from e


def from_plan(p: BuildPlan) -> WirePlan:
    """The inverse, so a retry shows the planner its previous plan in the format it writes."""
    s = p.stock
    stock = dict(kind=s.kind, size_x=0, size_y=0, size_z=0, corner_radius=0, diameter=0, height=0, plane="none",
                 points=[], depth=0, concave_corner_radius=0, convex_corner_radius=0)
    if s.kind == "box":
        stock.update(size_x=s.size.x, size_y=s.size.y, size_z=s.size.z, corner_radius=s.corner_radius or 0)
    elif s.kind == "cylinder":
        stock.update(diameter=s.diameter, height=s.height)
    else:
        stock.update(plane=s.plane, points=[[q.u, q.v] for q in s.points], depth=s.depth,
                     concave_corner_radius=s.concave_corner_radius or 0, convex_corner_radius=s.convex_corner_radius or 0)
    ops = []
    for o in p.operations:
        w = dict(op=o.op, id=o.id, dims=o.dims, direction="none", positions=[], bolt_circle=[], diameter=0, depth=0,
                 counterbore_diameter=0, counterbore_depth=0, center=[], length=0, width=0, corner_radius=0, angle_deg=0,
                 shape="none", height=0, corner_min=[], corner_max=[], corner_axis="none")
        if o.op == "hole":
            bc = o.bolt_circle
            w.update(direction=o.direction, positions=[[q.x, q.y, q.z] for q in o.positions], diameter=o.diameter,
                     bolt_circle=[bc.center.x, bc.center.y, bc.center.z, bc.diameter, bc.count, bc.start_angle_deg] if bc else [],
                     depth=o.depth or 0, counterbore_diameter=o.counterbore_diameter or 0, counterbore_depth=o.counterbore_depth or 0)
        elif o.op == "pocket":
            w.update(direction=o.direction, center=[o.center.x, o.center.y, o.center.z], length=o.length, width=o.width,
                     depth=o.depth or 0, corner_radius=o.corner_radius or 0, angle_deg=o.angle_deg or 0)
        elif o.op == "boss":
            w.update(direction=o.direction, center=[o.base_center.x, o.base_center.y, o.base_center.z], shape=o.shape,
                     height=o.height, diameter=o.diameter or 0, length=o.length or 0, width=o.width or 0)
        else:
            w.update(corner_min=[o.corner_min.x, o.corner_min.y, o.corner_min.z],
                     corner_max=[o.corner_max.x, o.corner_max.y, o.corner_max.z],
                     corner_radius=o.corner_radius or 0, corner_axis=o.corner_axis or "none")
        ops.append(w)
    return WirePlan.model_validate({"stock": stock, "operations": ops, "assumptions": p.assumptions})
