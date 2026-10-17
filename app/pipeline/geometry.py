"""Geometry stage: deterministic facts from a STEP file via OpenCascade.

Everything here is measured, not inferred. Feature recognition is deliberately
limited to candidates (full concave cylinders = hole candidates, partial ones =
internal radii, concave plane/plane edges = sharp internal corners). Pocket and
undercut recognition are out of scope for Phase 1 and are reported as skipped
checks downstream.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import (
    GeomAbs_BSplineSurface,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Line,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.Message import Message, Message_Gravity
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape
try:  # OCP >= 7.8 moved NCollection typedefs
    from OCP.OCP.collections import (
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as TopTools_IndexedDataMapOfShapeListOfShape,
    )
except ImportError:
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d, gp_Vec

from app.schemas import (
    ConcaveRadius,
    GeometrySummary,
    HoleCandidate,
    SharpInternalEdge,
    Vec3,
    WallThickness,
)

_SURFACE_NAMES = {
    GeomAbs_Plane: "plane",
    GeomAbs_Cylinder: "cylinder",
    GeomAbs_Cone: "cone",
    GeomAbs_Sphere: "sphere",
    GeomAbs_Torus: "torus",
    GeomAbs_BSplineSurface: "bspline",
}

TWO_PI = 2 * math.pi

# OCCT 7.9+ turned TopoDS into a namespace (TopoDS.Face); older OCP exposes TopoDS.Face_s.
_as_face = getattr(TopoDS, "Face", None) or TopoDS.Face_s
_as_edge = getattr(TopoDS, "Edge", None) or TopoDS.Edge_s
_as_solid = getattr(TopoDS, "Solid", None) or TopoDS.Solid_s
MAX_WALL_SAMPLES = 2000


class GeometryError(Exception):
    pass


# OCCT prints transfer statistics to stdout; keep only failures.
for _printer in Message.DefaultMessenger_s().Printers():
    _printer.SetTraceLevel(Message_Gravity.Message_Fail)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_step(path: str | Path) -> TopoDS_Shape:
    """Read a STEP file. OCCT converts lengths to millimetres on transfer."""
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise GeometryError(f"Could not read STEP file: {Path(path).name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise GeometryError("STEP file contains no transferable geometry.")
    return shape


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _iter(shape: TopoDS_Shape, kind, cast):
    exp = TopExp_Explorer(shape, kind)
    while exp.More():
        yield cast(exp.Current())
        exp.Next()


def _faces(shape):
    return _iter(shape, TopAbs_FACE, _as_face)


def _solids(shape):
    return list(_iter(shape, TopAbs_SOLID, _as_solid))


def _v(p) -> Vec3:
    return Vec3(x=round(p.X(), 4), y=round(p.Y(), 4), z=round(p.Z(), 4))


def _outward_normal(face: TopoDS_Face, surf: BRepAdaptor_Surface, u: float, v: float) -> tuple[gp_Pnt, gp_Dir] | None:
    props = BRepLProp_SLProps(surf, u, v, 1, 1e-6)
    if not props.IsNormalDefined():
        return None
    n = props.Normal()
    if face.Orientation() == TopAbs_REVERSED:
        n.Reverse()
    return props.Value(), n


def _canonical_dir(d: gp_Dir) -> tuple[float, float, float]:
    """Direction with sign fixed so +d and -d compare equal."""
    c = (d.X(), d.Y(), d.Z())
    for comp in c:
        if abs(comp) > 1e-9:
            return c if comp > 0 else (-c[0], -c[1], -c[2])
    return c


# --------------------------------------------------------------------------
# Feature candidates
# --------------------------------------------------------------------------


def _concave_cylinders(shape) -> tuple[list[HoleCandidate], list[ConcaveRadius]]:
    """Group concave cylindrical faces by (axis line, radius).

    A group whose angular spans add to a full turn is a hole candidate (CAD
    systems often split holes into two half-faces). Anything less is an
    internal radius, e.g. a pocket corner fillet.
    """
    groups: dict[tuple, dict] = defaultdict(lambda: {"span": 0.0, "vmin": math.inf, "vmax": -math.inf})
    for face in _faces(shape):
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        cyl = surf.Cylinder()
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        um, vm = (umin + umax) / 2, (vmin + vmax) / 2
        on = _outward_normal(face, surf, um, vm)
        if on is None:
            continue
        p, n = on
        axis = cyl.Axis()
        o, d = axis.Location(), axis.Direction()
        op = gp_Vec(o, p)
        radial = op - gp_Vec(d).Multiplied(op.Dot(gp_Vec(d)))
        if radial.Magnitude() < 1e-9 or radial.Dot(gp_Vec(n)) >= 0:
            continue  # convex (boss / outer fillet)

        dc = _canonical_dir(d)
        dv = gp_Vec(*dc)
        ov = gp_Vec(o.X(), o.Y(), o.Z())
        foot = ov - dv.Multiplied(ov.Dot(dv))  # axis line's closest point to origin
        key = (round(cyl.Radius(), 3), tuple(round(c, 4) for c in dc), round(foot.X(), 2), round(foot.Y(), 2), round(foot.Z(), 2))
        g = groups[key]
        g["span"] += umax - umin
        # Axial extent: project the face's two ends onto the canonical axis.
        for vv in (vmin, vmax):
            end = surf.Value(um, vv)
            s = gp_Vec(end.X(), end.Y(), end.Z()).Dot(dv)
            g["vmin"], g["vmax"] = min(g["vmin"], s), max(g["vmax"], s)

    holes: list[HoleCandidate] = []
    radii: list[ConcaveRadius] = []
    for (r, dc, fx, fy, fz), g in groups.items():
        axis = Vec3(x=dc[0], y=dc[1], z=dc[2])
        if g["span"] >= TWO_PI - 1e-3:
            mid = (g["vmin"] + g["vmax"]) / 2
            holes.append(HoleCandidate(
                diameter_mm=round(2 * r, 4),
                depth_mm=round(g["vmax"] - g["vmin"], 4),
                axis=axis,
                center=Vec3(x=round(fx + dc[0] * mid, 4), y=round(fy + dc[1] * mid, 4), z=round(fz + dc[2] * mid, 4)),
            ))
        else:
            radii.append(ConcaveRadius(radius_mm=r, axis=axis))
    return holes, radii


def _sharp_internal_edges(solid) -> list[SharpInternalEdge]:
    """Straight edges between two planar faces where the material is concave.

    Test: from the edge midpoint step a little along (n1 - n2). For a convex
    edge that point is outside the solid; for a concave edge it is inside.
    """
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    out: list[SharpInternalEdge] = []
    for i in range(1, edge_faces.Extent() + 1):
        edge = _as_edge(edge_faces.FindKey(i))
        faces = list(edge_faces.FindFromIndex(i))
        if len(faces) != 2 or faces[0].IsSame(faces[1]):
            continue
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() != GeomAbs_Line:
            continue
        normals = []
        for f in faces:
            face = _as_face(f)
            surf = BRepAdaptor_Surface(face)
            if surf.GetType() != GeomAbs_Plane:
                break
            n = surf.Plane().Axis().Direction()
            if face.Orientation() == TopAbs_REVERSED:
                n.Reverse()
            normals.append(gp_Vec(n))
        if len(normals) != 2:
            continue
        diff = normals[0] - normals[1]
        if diff.Magnitude() < 1e-6 or (normals[0] + normals[1]).Magnitude() < 1e-6:
            continue  # coplanar or antiparallel
        t0, t1 = curve.FirstParameter(), curve.LastParameter()
        length = abs(t1 - t0)
        mid = curve.Value((t0 + t1) / 2)
        eps = min(0.05, length * 0.1)
        q = mid.Translated(diff.Normalized().Multiplied(eps))
        if BRepClass3d_SolidClassifier(solid, q, 1e-7).State() == TopAbs_IN:
            out.append(SharpInternalEdge(length_mm=round(length, 4), direction=_v(curve.Line().Direction()), midpoint=_v(mid)))
    return out


# --------------------------------------------------------------------------
# Wall thickness
# --------------------------------------------------------------------------


def _wall_thickness(shape) -> WallThickness:
    """Shoot a ray inward from sampled face points; first exit = local thickness."""
    faces = list(_faces(shape))
    per_face = max(1, min(9, MAX_WALL_SAMPLES // max(1, len(faces))))
    grid = max(1, int(math.sqrt(per_face)))
    fractions = [0.5] if grid == 1 else [0.2 + 0.6 * k / (grid - 1) for k in range(grid)]

    inter = IntCurvesFace_ShapeIntersector()
    inter.Load(shape, 1e-6)
    distances: list[float] = []
    for face in faces:
        surf = BRepAdaptor_Surface(face)
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        classifier = BRepClass_FaceClassifier()
        for fu in fractions:
            for fv in fractions:
                u, v = umin + fu * (umax - umin), vmin + fv * (vmax - vmin)
                classifier.Perform(face, gp_Pnt2d(u, v), 1e-6)
                if classifier.State() != TopAbs_IN:
                    continue
                on = _outward_normal(face, surf, u, v)
                if on is None:
                    continue
                p, n = on
                inward = n.Reversed()
                start = p.Translated(gp_Vec(inward).Multiplied(1e-4))
                inter.Perform(gp_Lin(start, inward), 0.0, 1e9)
                hits = [inter.WParameter(k) for k in range(1, inter.NbPnt() + 1)]
                hits = [w for w in hits if w > 1e-3]
                if hits:
                    distances.append(min(hits) + 1e-4)

    if not distances:
        return WallThickness(samples=0, min_mm=None, p05_mm=None)
    distances.sort()
    p05 = distances[max(0, int(len(distances) * 0.05) - 1)]
    return WallThickness(samples=len(distances), min_mm=round(distances[0], 3), p05_mm=round(p05, 3))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def summarize_shape(shape: TopoDS_Shape) -> GeometrySummary:
    warnings: list[str] = []
    solids = _solids(shape)
    if not solids:
        raise GeometryError("No solid bodies found (surfaces only?). A closed solid is required to check and quote.")
    if len(solids) > 1:
        warnings.append(f"{len(solids)} solid bodies found; treated as one part. Split assemblies into separate jobs.")

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    volume = props.Mass()
    sprops = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, sprops)

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    lo, hi = box.CornerMin(), box.CornerMax()  # Get() is unbound in OCP 8

    counts = Counter(_SURFACE_NAMES.get(BRepAdaptor_Surface(f).GetType(), "other") for f in _faces(shape))
    holes, radii = _concave_cylinders(shape)
    sharp = [e for s in solids for e in _sharp_internal_edges(s)]

    warnings.append("Bounding box is axis-aligned in the file's coordinates; stock may be smaller if the part is rotated.")
    return GeometrySummary(
        solid_count=len(solids),
        bbox_mm=Vec3(x=round(hi.X() - lo.X(), 4), y=round(hi.Y() - lo.Y(), 4), z=round(hi.Z() - lo.Z(), 4)),
        volume_mm3=round(volume, 3),
        surface_area_mm2=round(sprops.Mass(), 3),
        face_type_counts=dict(counts),
        hole_candidates=holes,
        concave_radii=radii,
        sharp_internal_edges=sharp,
        wall_thickness=_wall_thickness(shape),
        warnings=warnings,
    )


def run(step_path: str | Path) -> GeometrySummary:
    return summarize_shape(load_step(step_path))
