"""Deterministic builder: BuildPlan -> OpenCascade solid.

The planner (an LLM or a person) only ever produces a BuildPlan. This module
turns it into geometry with fixed, testable rules. It never decides whether the
result is right; verify.py does that independently.

Conventions:
- Stock is placed with its bounding-box minimum corner at the origin.
- Cutting tools start 0.5 mm outside the entry face so booleans never meet
  coplanar faces; "through" tools run the full part diagonal both ways.
- Holes have flat bottoms (no drill point).
"""

from __future__ import annotations

import math

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet, BRepFilletAPI_MakeFillet2d
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.GeomAbs import GeomAbs_Line
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

from app.build.schemas import (
    BossOp,
    BuildPlan,
    CutBoxOp,
    HoleOp,
    PocketOp,
    StockBox,
    StockCylinder,
    StockExtrusion,
)
from app.schemas import Vec3

_as_edge = getattr(TopoDS, "Edge", None) or TopoDS.Edge_s
_as_vertex = getattr(TopoDS, "Vertex", None) or TopoDS.Vertex_s
_as_face = getattr(TopoDS, "Face", None) or TopoDS.Face_s

EXT = 0.5  # mm a tool starts outside its entry face
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
UNIT = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
IN_PLANE = {"z": ("x", "y"), "x": ("y", "z"), "y": ("x", "z")}  # first / second in-plane axis


class BuildError(Exception):
    """A plan that can't be built. The message goes back to the planner."""


def direction(d: str) -> tuple[str, float]:
    return d[1], (1.0 if d[0] == "+" else -1.0)


def dir_vec(d: str) -> tuple[float, float, float]:
    axis, s = direction(d)
    return tuple(s * c for c in UNIT[axis])  # type: ignore[return-value]


def in_plane_axes(d: str) -> tuple[str, str]:
    return IN_PLANE[direction(d)[0]]


def _p(v: Vec3 | tuple[float, float, float]) -> gp_Pnt:
    if isinstance(v, Vec3):
        return gp_Pnt(v.x, v.y, v.z)
    return gp_Pnt(*v)


def _tup(v: Vec3) -> tuple[float, float, float]:
    return (v.x, v.y, v.z)


def _add(a, b, k=1.0):
    return tuple(a[i] + k * b[i] for i in range(3))


def _box(lo, hi) -> TopoDS_Shape:
    lo2 = tuple(min(lo[i], hi[i]) for i in range(3))
    hi2 = tuple(max(lo[i], hi[i]) for i in range(3))
    if any(hi2[i] - lo2[i] <= 1e-9 for i in range(3)):
        raise BuildError(f"zero-size box {lo2} to {hi2}")
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo2), gp_Pnt(*hi2)).Shape()


def _cylinder(start, d: str | tuple, radius: float, height: float) -> TopoDS_Shape:
    vec = dir_vec(d) if isinstance(d, str) else d
    if radius <= 0 or height <= 0:
        raise BuildError(f"cylinder needs positive radius and height (got r={radius}, h={height})")
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start), gp_Dir(*vec)), radius, height).Shape()


def _boolean(op, a, b, what: str) -> TopoDS_Shape:
    algo = op(a, b)
    algo.Build()
    if not algo.IsDone():
        raise BuildError(f"boolean failed while applying {what}")
    return algo.Shape()


def _fillet_edges_parallel(shape, axis: str, radius: float, what: str) -> TopoDS_Shape:
    """Fillet every straight edge of `shape` parallel to `axis`."""
    target = gp_Dir(*UNIT[axis])
    mk = BRepFilletAPI_MakeFillet(shape)
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    seen = []
    while exp.More():
        e = _as_edge(exp.Current())
        exp.Next()
        if any(e.IsSame(s) for s in seen):
            continue
        seen.append(e)
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_Line and c.Line().Direction().IsParallel(target, 1e-6):
            mk.Add(radius, e)
            n += 1
    if n == 0:
        return shape
    mk.Build()
    if not mk.IsDone():
        raise BuildError(f"corner radius {radius} failed on {what} (radius too large for the feature?)")
    return mk.Shape()


def _bbox(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    lo, hi = box.CornerMin(), box.CornerMax()
    return (lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z())


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------


def _stock_box(s: StockBox) -> TopoDS_Shape:
    x, y, z = s.size.x, s.size.y, s.size.z
    if min(x, y, z) <= 0:
        raise BuildError("stock box sizes must be positive")
    shape = _box((0, 0, 0), (x, y, z))
    if s.corner_radius:
        if s.corner_radius >= min(x, y) / 2:
            raise BuildError(f"stock corner radius {s.corner_radius} must be less than half the smaller side ({min(x, y) / 2})")
        shape = _fillet_edges_parallel(shape, "z", s.corner_radius, "stock corners")
    return shape


def _stock_cylinder(s: StockCylinder) -> TopoDS_Shape:
    r = s.diameter / 2
    return _cylinder((r, r, 0.0), "+z", r, s.height)


def _profile_point(plane: str, u: float, v: float) -> tuple[float, float, float]:
    return {"xy": (u, v, 0.0), "xz": (u, 0.0, v), "yz": (0.0, u, v)}[plane]


def _stock_extrusion(s: StockExtrusion) -> TopoDS_Shape:
    pts = [(p.u, p.v) for p in s.points]
    if len(pts) < 3:
        raise BuildError("extrusion outline needs at least 3 points")
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    if s.depth <= 0:
        raise BuildError("extrusion depth must be positive")
    area2 = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
    if abs(area2) < 1e-9:
        raise BuildError("extrusion outline has zero area")
    ccw = area2 > 0

    poly = BRepBuilderAPI_MakePolygon()
    for u, v in pts:
        poly.Add(gp_Pnt(*_profile_point(s.plane, u, v)))
    poly.Close()
    if not poly.IsDone():
        raise BuildError("extrusion outline is not a valid polygon")
    face_mk = BRepBuilderAPI_MakeFace(poly.Wire(), True)
    if not face_mk.IsDone():
        raise BuildError("extrusion outline is not planar or self-intersects")
    face = face_mk.Face()

    if s.concave_corner_radius or s.convex_corner_radius:
        # Classify each outline corner, then fillet the matching face vertices.
        corner_r: dict[tuple[float, float, float], float] = {}
        n = len(pts)
        for i in range(n):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            convex = (cross > 0) == ccw
            r = s.convex_corner_radius if convex else s.concave_corner_radius
            if r:
                corner_r[tuple(round(k, 6) for k in _profile_point(s.plane, *b))] = r
        f2d = BRepFilletAPI_MakeFillet2d(face)
        exp = TopExp_Explorer(face, TopAbs_VERTEX)
        done = []
        while exp.More():
            vtx = _as_vertex(exp.Current())
            exp.Next()
            if any(vtx.IsSame(d) for d in done):
                continue
            done.append(vtx)
            p = BRep_Tool.Pnt_s(vtx)
            r = corner_r.get((round(p.X(), 6), round(p.Y(), 6), round(p.Z(), 6)))
            if r:
                f2d.AddFillet(vtx, r)
        f2d.Build()
        if not f2d.IsDone():
            raise BuildError("outline corner radius failed (radius larger than an adjacent edge?)")
        face = _as_face(f2d.Shape())

    vec = {"xy": (0, 0, s.depth), "xz": (0, s.depth, 0), "yz": (s.depth, 0, 0)}[s.plane]
    return BRepPrimAPI_MakePrism(face, gp_Vec(*vec)).Shape()


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def hole_positions(op: HoleOp) -> list[tuple[float, float, float]]:
    pts = [_tup(p) for p in op.positions]
    if op.bolt_circle:
        bc = op.bolt_circle
        if bc.count < 1 or bc.diameter <= 0:
            raise BuildError(f"{op.id}: bolt circle needs count ≥ 1 and a positive diameter")
        ua, va = in_plane_axes(op.direction)
        c = _tup(bc.center)
        for i in range(bc.count):
            th = math.radians(bc.start_angle_deg + 360.0 * i / bc.count)
            p = list(c)
            p[AXIS_INDEX[ua]] += bc.diameter / 2 * math.cos(th)
            p[AXIS_INDEX[va]] += bc.diameter / 2 * math.sin(th)
            pts.append(tuple(p))
    if not pts:
        raise BuildError(f"{op.id}: hole has no positions")
    return pts


def _hole_tools(op: HoleOp, diag: float) -> list[TopoDS_Shape]:
    if op.diameter <= 0:
        raise BuildError(f"{op.id}: hole diameter must be positive")
    if op.depth is not None and op.depth <= 0:
        raise BuildError(f"{op.id}: hole depth must be positive (use null for through)")
    cb = op.counterbore_diameter is not None or op.counterbore_depth is not None
    if cb:
        if not (op.counterbore_diameter and op.counterbore_depth):
            raise BuildError(f"{op.id}: counterbore needs both diameter and depth")
        if op.counterbore_diameter <= op.diameter:
            raise BuildError(f"{op.id}: counterbore diameter must exceed the hole diameter")
        if op.depth is not None and op.counterbore_depth >= op.depth:
            raise BuildError(f"{op.id}: counterbore depth must be less than the hole depth")
    d = dir_vec(op.direction)
    tools = []
    for p in hole_positions(op):
        if op.depth is None:
            tools.append(_cylinder(_add(p, d, -diag), d, op.diameter / 2, 2 * diag))
        else:
            tools.append(_cylinder(_add(p, d, -EXT), d, op.diameter / 2, op.depth + EXT))
        if cb:
            tools.append(_cylinder(_add(p, d, -EXT), d, op.counterbore_diameter / 2, op.counterbore_depth + EXT))
    return tools


def _rect_tool(center, d: str, length: float, width: float, start: float, end: float,
               corner_radius: float | None, angle_deg: float | None, what: str) -> TopoDS_Shape:
    """Prism with a (rounded) rectangular section, spanning [start, end] along d from center."""
    if length <= 0 or width <= 0:
        raise BuildError(f"{what}: length and width must be positive")
    axis, s = direction(d)
    ua, va = in_plane_axes(d)
    ai, ui, vi = AXIS_INDEX[axis], AXIS_INDEX[ua], AXIS_INDEX[va]
    r = corner_radius or 0.0
    half = min(length, width) / 2
    if r < 0:
        raise BuildError(f"{what}: corner radius can't be negative")
    if r > half + 1e-9:
        raise BuildError(f"{what}: corner radius {r} exceeds half the smaller side ({half})")

    def span_box(lu: float, lv: float):
        lo, hi = list(center), list(center)
        lo[ui], hi[ui] = center[ui] - lu / 2, center[ui] + lu / 2
        lo[vi], hi[vi] = center[vi] - lv / 2, center[vi] + lv / 2
        lo[ai], hi[ai] = center[ai] + s * start, center[ai] + s * end
        return _box(lo, hi)

    if r >= half - 1e-9 and r > 0:
        # Obround: a box between two full-round ends along the longer side.
        long_axis_i, long_len, short_len = (ui, length, width) if length >= width else (vi, width, length)
        base = list(center)
        base[ai] += s * start
        if long_len - short_len < 1e-9:  # equal sides: a plain round pocket
            tool = _cylinder(tuple(base), d, short_len / 2, end - start)
        else:
            tool = span_box(length - short_len if long_axis_i == ui else length,
                            width - short_len if long_axis_i == vi else width)
            for k in (-1, 1):
                c = list(base)
                c[long_axis_i] += k * (long_len - short_len) / 2
                tool = _boolean(BRepAlgoAPI_Fuse, tool, _cylinder(tuple(c), d, short_len / 2, end - start), what)
    else:
        tool = span_box(length, width)
        if r > 0:
            tool = _fillet_edges_parallel(tool, axis, r, what)
    if angle_deg:
        t = gp_Trsf()
        t.SetRotation(gp_Ax1(gp_Pnt(*center), gp_Dir(*UNIT[axis])), math.radians(angle_deg))
        tool = BRepBuilderAPI_Transform(tool, t, True).Shape()
    return tool


def _pocket_tool(op: PocketOp, diag: float) -> TopoDS_Shape:
    if op.depth is not None and op.depth <= 0:
        raise BuildError(f"{op.id}: pocket depth must be positive (use null for through)")
    end = diag if op.depth is None else op.depth
    start = -diag if op.depth is None else -EXT
    return _rect_tool(_tup(op.center), op.direction, op.length, op.width, start, end, op.corner_radius, op.angle_deg, op.id)


def _boss_tool(op: BossOp) -> TopoDS_Shape:
    if op.height <= 0:
        raise BuildError(f"{op.id}: boss height must be positive")
    c = _tup(op.base_center)
    if op.shape == "cylinder":
        if not op.diameter:
            raise BuildError(f"{op.id}: cylinder boss needs a diameter")
        d = dir_vec(op.direction)
        return _cylinder(_add(c, d, -EXT), d, op.diameter / 2, op.height + EXT)
    if not (op.length and op.width):
        raise BuildError(f"{op.id}: box boss needs length and width")
    return _rect_tool(c, op.direction, op.length, op.width, -EXT, op.height, None, None, op.id)


def _cut_box_tool(op: CutBoxOp, part_lo, part_hi) -> TopoDS_Shape:
    lo, hi = list(_tup(op.corner_min)), list(_tup(op.corner_max))
    if op.corner_radius:
        # Where the cut leaves the part, push the tool well past it so only true
        # inside corners get rounded (rounded exit corners would leave slivers).
        skip = AXIS_INDEX[op.corner_axis or "z"]
        for i in range(3):
            if i == skip:
                continue
            if lo[i] <= part_lo[i] + 1e-9:
                lo[i] = part_lo[i] - op.corner_radius - 1.0
            if hi[i] >= part_hi[i] - 1e-9:
                hi[i] = part_hi[i] + op.corner_radius + 1.0
    tool = _box(tuple(lo), tuple(hi))
    if op.corner_radius:
        tool = _fillet_edges_parallel(tool, op.corner_axis or "z", op.corner_radius, op.id)
    return tool


def build(plan: BuildPlan) -> TopoDS_Shape:
    s = plan.stock
    shape = {"box": _stock_box, "cylinder": _stock_cylinder, "extrusion": _stock_extrusion}[s.kind](s)  # type: ignore[arg-type]

    for op in plan.operations:
        lo, hi = _bbox(shape)
        diag = math.dist(lo, hi) + 10.0
        if isinstance(op, HoleOp):
            for t in _hole_tools(op, diag):
                shape = _boolean(BRepAlgoAPI_Cut, shape, t, op.id)
        elif isinstance(op, PocketOp):
            shape = _boolean(BRepAlgoAPI_Cut, shape, _pocket_tool(op, diag), op.id)
        elif isinstance(op, BossOp):
            shape = _boolean(BRepAlgoAPI_Fuse, shape, _boss_tool(op), op.id)
        elif isinstance(op, CutBoxOp):
            shape = _boolean(BRepAlgoAPI_Cut, shape, _cut_box_tool(op, lo, hi), op.id)
        else:  # pragma: no cover - the schema forbids it
            raise BuildError(f"unknown operation {op}")

    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    return unify.Shape()
