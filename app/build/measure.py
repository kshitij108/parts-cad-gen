"""Measurement primitives on a solid, in datum coordinates (bbox minimum = 0).

These know nothing about plans or planners: they take a shape and a question
("how much material along X through this point?") and answer from the B-rep.
That independence is what makes verification meaningful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_Cylinder
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

from app.pipeline.geometry import concave_cylinders
from app.schemas import HoleCandidate, Vec3

_as_face = getattr(TopoDS, "Face", None) or TopoDS.Face_s
_as_solid = getattr(TopoDS, "Solid", None) or TopoDS.Solid_s

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
UNIT = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
FAR = 1e5


@dataclass
class Segment:
    t0: float  # along the probe axis, in datum coordinates
    t1: float
    inside: bool

    @property
    def length(self) -> float:
        return self.t1 - self.t0


@dataclass
class Hole:
    diameter: float
    depth: float
    axis: tuple[float, float, float]
    center: tuple[float, float, float]  # datum coordinates, mid-depth on the axis

    def label(self) -> str:
        c = self.center
        return f"Ø{self.diameter:.3f} at ({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f})"


class Measurer:
    """Caches the expensive bits (bbox, solids, holes, cylinders) for one shape."""

    def __init__(self, shape):
        self.shape = shape
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(shape, box, False, False)
        lo, hi = box.CornerMin(), box.CornerMax()
        self.lo = (lo.X(), lo.Y(), lo.Z())
        self.hi = (hi.X(), hi.Y(), hi.Z())
        self.size = tuple(self.hi[i] - self.lo[i] for i in range(3))
        self.solids = []
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            self.solids.append(_as_solid(exp.Current()))
            exp.Next()
        self._inter = IntCurvesFace_ShapeIntersector()
        self._inter.Load(shape, 1e-7)
        self._holes: list[Hole] | None = None
        self._cyl_faces: list | None = None

    # ---------------------------------------------------------------- coords

    def to_abs(self, p: Vec3 | tuple) -> tuple[float, float, float]:
        q = (p.x, p.y, p.z) if isinstance(p, Vec3) else p
        return tuple(q[i] + self.lo[i] for i in range(3))  # type: ignore[return-value]

    def to_rel(self, p: tuple) -> tuple[float, float, float]:
        return tuple(p[i] - self.lo[i] for i in range(3))  # type: ignore[return-value]

    def inside(self, p_abs: tuple) -> bool:
        pt = gp_Pnt(*p_abs)
        return any(BRepClass3d_SolidClassifier(s, pt, 1e-7).State() == TopAbs_IN for s in self.solids)

    # ----------------------------------------------------------------- probes

    def probe(self, at: Vec3 | tuple, axis: str) -> list[Segment]:
        """Material/empty segments along a line through `at` parallel to `axis`."""
        p = self.to_abs(at)
        ai = AXIS_INDEX[axis]
        origin = list(p)
        origin[ai] = 0.0
        self._inter.Perform(gp_Lin(gp_Pnt(*origin), gp_Dir(*UNIT[axis])), -FAR, FAR)
        ts = sorted(self._inter.WParameter(k) for k in range(1, self._inter.NbPnt() + 1))
        merged: list[float] = []
        for t in ts:
            if not merged or t - merged[-1] > 1e-6:
                merged.append(t)
        bounds = [-FAR] + merged + [FAR]
        segs: list[Segment] = []
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 1e-9:
                continue
            mid = list(origin)
            mid[ai] = (a + b) / 2 if abs(a) < FAR and abs(b) < FAR else (b - 1.0 if a <= -FAR else a + 1.0)
            inside = self.inside(tuple(mid))
            if segs and segs[-1].inside == inside:
                segs[-1].t1 = b - self.lo[ai]
            else:
                segs.append(Segment(a - self.lo[ai], b - self.lo[ai], inside))
        return segs

    def segment_at(self, at: Vec3, axis: str) -> tuple[Segment | None, list[Segment]]:
        segs = self.probe(at, axis)
        t = (at.x, at.y, at.z)[AXIS_INDEX[axis]]
        for s in segs:
            if s.t0 - 1e-9 <= t <= s.t1 + 1e-9:
                return s, segs
        return None, segs

    # ------------------------------------------------------------------ holes

    @property
    def holes(self) -> list[Hole]:
        if self._holes is None:
            cands, _ = concave_cylinders(self.shape)
            self._holes = [Hole(h.diameter_mm, h.depth_mm, (h.axis.x, h.axis.y, h.axis.z),
                                self.to_rel((h.center.x, h.center.y, h.center.z))) for h in cands]
        return self._holes

    def hole_near(self, at: Vec3, diameter: float | None = None) -> tuple[Hole | None, float]:
        """Hole whose axis passes nearest `at` (within max(2 mm, its radius)).

        When several holes share a centre (counterbores), the one whose
        diameter is closest to `diameter` wins.
        """
        p = (at.x, at.y, at.z)
        best: tuple[float, float, Hole] | None = None
        for h in self.holes:
            v = [p[i] - h.center[i] for i in range(3)]
            along = sum(v[i] * h.axis[i] for i in range(3))
            off = math.sqrt(max(0.0, sum(c * c for c in v) - along * along))
            if off > max(2.0, h.diameter / 2):
                continue
            key = (abs(h.diameter - diameter) if diameter is not None else 0.0, off)
            if best is None or key < best[:2]:
                best = (*key, h)
        return (best[2], best[1]) if best else (None, math.inf)

    def hole_is_through(self, h: Hole) -> bool:
        """Through if there's no material just past either end of the hole wall.

        Samples sit in the ring between this hole and any smaller hole on the
        same axis, so a counterbore (whose floor is pierced by the through
        hole) still reads as blind.
        """
        inner = 0.0
        for o in self.holes:
            if o is h or o.diameter >= h.diameter:
                continue
            if abs(abs(sum(o.axis[i] * h.axis[i] for i in range(3))) - 1) > 1e-6:
                continue
            v = [o.center[i] - h.center[i] for i in range(3)]
            along = sum(v[i] * h.axis[i] for i in range(3))
            if sum(c * c for c in v) - along * along < 1e-8:
                inner = max(inner, o.diameter / 2)
        r = h.diameter / 2
        offset = (r + inner) / 2 if inner else r / 2
        # Two perpendicular radial directions.
        a = h.axis
        helper = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = (a[1] * helper[2] - a[2] * helper[1], a[2] * helper[0] - a[0] * helper[2], a[0] * helper[1] - a[1] * helper[0])
        un = math.sqrt(sum(c * c for c in u))
        u = tuple(c / un for c in u)
        w = (a[1] * u[2] - a[2] * u[1], a[2] * u[0] - a[0] * u[2], a[0] * u[1] - a[1] * u[0])
        c = self.to_abs(h.center)
        for s in (-1, 1):
            for radial in (u, w):
                end = tuple(c[i] + s * (h.depth / 2 + 0.05) * a[i] + offset * radial[i] for i in range(3))
                if self.inside(end):
                    return False
        return True

    # ------------------------------------------------------------- cylinders

    def nearest_cylinder(self, at: Vec3, axis: str) -> tuple[float | None, float]:
        """Radius of the cylindrical face nearest `at` with axis parallel to `axis`."""
        if self._cyl_faces is None:
            self._cyl_faces = []
            exp = TopExp_Explorer(self.shape, TopAbs_FACE)
            while exp.More():
                f = _as_face(exp.Current())
                exp.Next()
                surf = BRepAdaptor_Surface(f)
                if surf.GetType() == GeomAbs_Cylinder:
                    cyl = surf.Cylinder()
                    d = cyl.Axis().Direction()
                    self._cyl_faces.append((f, cyl.Radius(), (d.X(), d.Y(), d.Z())))
        target = UNIT[axis]
        vtx = BRepBuilderAPI_MakeVertex(gp_Pnt(*self.to_abs(at))).Vertex()
        best: tuple[float, float] | None = None
        for f, r, d in self._cyl_faces:
            if abs(sum(d[i] * target[i] for i in range(3))) < 0.999:
                continue
            dist = BRepExtrema_DistShapeShape(vtx, f)
            if not dist.IsDone():
                continue
            if best is None or dist.Value() < best[1]:
                best = (r, dist.Value())
        return best if best else (None, math.inf)
