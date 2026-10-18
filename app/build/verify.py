"""Verification: measure a solid against a confirmed DimensionSpec.

This is the gate in "always validate generated geometry before showing it".
It uses only the solid and the spec; the plan is consulted solely for
bookkeeping checks (expected hole count, coverage tags), never to decide what a
dimension measures.

A CAD model is ideal geometry, so it must sit on nominal: the pass band is
MODEL_TOL_MM, not the drawing's manufacturing tolerance (which is carried
through for DFM and quoting).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp

from app.build.builder import hole_positions
from app.build.measure import AXIS_INDEX, Measurer
from app.build.schemas import (
    BuildPlan,
    CheckResult,
    CoverageNote,
    DimensionSpec,
    HoleOp,
    RequiredDimension,
    ValidityCheck,
    VerificationReport,
)
from app.pipeline.geometry import GeometryError, load_step

try:  # OCP >= 7.8 moved NCollection typedefs
    from OCP.OCP.collections import (
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as EdgeFaceMap,
    )
except ImportError:  # pragma: no cover
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape as EdgeFaceMap

MODEL_TOL_MM = 0.01


def _volume(shape) -> float:
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


def write_step(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    if w.Write(str(path)) != IFSelect_RetDone:
        raise IOError(f"could not write {path}")
    return path


# --------------------------------------------------------------------------
# Validity
# --------------------------------------------------------------------------


def validity_checks(shape, m: Measurer) -> list[ValidityCheck]:
    out: list[ValidityCheck] = []
    valid = BRepCheck_Analyzer(shape).IsValid()
    out.append(ValidityCheck(name="valid_brep", passed=valid,
                             detail="OpenCascade topology check passed." if valid else "BRepCheck_Analyzer reports invalid topology."))
    n = len(m.solids)
    out.append(ValidityCheck(name="single_solid", passed=n == 1,
                             detail="One solid body." if n == 1 else f"{n} solid bodies (a feature split the part, or nothing was built)."))
    edge_faces = EdgeFaceMap()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    free = sum(1 for i in range(1, edge_faces.Extent() + 1) if len(list(edge_faces.FindFromIndex(i))) < 2)
    out.append(ValidityCheck(name="closed", passed=free == 0,
                             detail="Every edge is shared by two faces." if free == 0 else f"{free} free edges: the shell is open."))
    vol = _volume(shape)
    out.append(ValidityCheck(name="positive_volume", passed=vol > 1e-6, detail=f"Volume {vol:,.3f} mm³."))
    with tempfile.TemporaryDirectory() as d:
        try:
            back = load_step(write_step(shape, Path(d) / "roundtrip.step"))
            v2 = _volume(back)
            ok = abs(v2 - vol) <= 1e-6 * max(1.0, abs(vol))
            out.append(ValidityCheck(name="step_roundtrip", passed=ok,
                                     detail=f"Re-read volume {v2:,.3f} mm³" + ("" if ok else f" differs from {vol:,.3f} mm³.")))
        except (GeometryError, IOError) as e:
            out.append(ValidityCheck(name="step_roundtrip", passed=False, detail=str(e)))
    return out


# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def _result(d: RequiredDimension, measured: float | None, method: str, tol: float,
            expected_text: str | None = None, measured_text: str | None = None, passed: bool | None = None) -> CheckResult:
    if passed is None:
        passed = measured is not None and d.nominal is not None and abs(measured - d.nominal) <= tol
    dev = (measured - d.nominal) if (measured is not None and d.nominal is not None) else None
    return CheckResult(
        dim_id=d.id, label=d.label, kind=d.kind,
        expected=expected_text or (_fmt(d.nominal) if d.nominal is not None else "—"),
        measured=measured_text or (_fmt(measured) if measured is not None else None),
        measured_value=measured, deviation=round(dev, 6) if dev is not None else None,
        passed=passed, method=method,
    )


def _at(d: RequiredDimension) -> str:
    return f"({d.at.x:g}, {d.at.y:g}, {d.at.z:g})" if d.at else "(no point)"


def check_dimension(d: RequiredDimension, m: Measurer, tol: float = MODEL_TOL_MM) -> CheckResult:
    problems = d.problems()
    if problems:
        return _result(d, None, " ".join(problems), tol, passed=False)
    k = d.kind

    if k == "overall":
        v = m.size[AXIS_INDEX[d.axis]]
        return _result(d, v, f"Bounding box extent along {d.axis.upper()}.", tol)

    if k == "hole_count":
        n = sum(1 for h in m.holes if abs(h.diameter - d.feature_diameter) <= tol)
        return _result(d, float(n), f"Holes of Ø{d.feature_diameter:g} found: {n} (of {len(m.holes)} holes in the model).",
                       0.0, expected_text=f"{int(d.nominal)}", measured_text=str(n))

    if k in ("hole_diameter", "hole_depth", "hole_position"):
        want = d.nominal if k == "hole_diameter" else d.feature_diameter
        h, off = m.hole_near(d.at, want)
        if h is None:
            return _result(d, None, f"No hole axis passes within reach of {_at(d)}.", tol, passed=False)
        where = f"Matched hole {h.label()}, axis {off:.3f} mm from {_at(d)}."
        if k == "hole_diameter":
            return _result(d, h.diameter, where, tol)
        if k == "hole_depth":
            through = m.hole_is_through(h)
            if d.through:
                return _result(d, None, where + (" Open at both ends." if through else " Material found past one end."), tol,
                               expected_text="THRU", measured_text="THRU" if through else f"blind {h.depth:.3f}", passed=through)
            if through:
                return _result(d, h.depth, where + " The hole goes through, but a blind depth was expected.", tol,
                               measured_text=f"THRU ({h.depth:.3f})", passed=False)
            return _result(d, h.depth, where, tol)
        v = h.center[AXIS_INDEX[d.axis]]
        return _result(d, v, where + f" Centre {d.axis.upper()} coordinate from the datum.", tol)

    if k == "depth":
        axis, sign = d.direction[1], (1 if d.direction[0] == "+" else -1)
        segs = m.probe(d.at, axis)
        ai = AXIS_INDEX[axis]
        face = 0.0 if sign > 0 else m.size[ai]  # bounding face the depth starts from
        hits = [s for s in segs if s.inside]
        if not hits:
            entry = None
        elif sign < 0:
            entry = max(s.t1 for s in hits)
        else:
            entry = min(s.t0 for s in hits)
        line = f"Probe along {d.direction} at {_at(d)} from the {'min' if sign > 0 else 'max'} {axis.upper()} face."
        if d.through:
            return _result(d, None, line + (" No material on the line." if entry is None else f" Material at {entry:.3f}."), tol,
                           expected_text="THRU", measured_text="THRU" if entry is None else f"{abs(face - entry):.3f}",
                           passed=entry is None)
        if entry is None:
            return _result(d, None, line + " No material on the line (it goes through).", tol, measured_text="THRU", passed=False)
        return _result(d, abs(face - entry), line, tol)

    if k in ("material_width", "gap_width"):
        seg, segs = m.segment_at(d.at, d.axis)
        line = f"Probe along {d.axis.upper()} through {_at(d)}"
        if seg is None:
            return _result(d, None, line + ": point is outside the part's range.", tol, passed=False)
        if k == "material_width":
            if not seg.inside:
                return _result(d, None, line + ": the point is in empty space, not material.", tol, passed=False)
            return _result(d, seg.length, line + f": material from {seg.t0:.3f} to {seg.t1:.3f}.", tol)
        if seg.inside:
            return _result(d, None, line + ": the point is inside material, not a gap.", tol, passed=False)
        if seg is segs[0] or seg is segs[-1]:
            return _result(d, None, line + ": the gap is open (no material on one side).", tol, passed=False)
        return _result(d, seg.length, line + f": gap from {seg.t0:.3f} to {seg.t1:.3f}.", tol)

    if k == "radius":
        r, dist = m.nearest_cylinder(d.at, d.axis)
        if r is None or dist > 1.0:
            return _result(d, None, f"No cylindrical face with a {d.axis.upper()} axis within 1 mm of {_at(d)}.", tol, passed=False)
        return _result(d, r, f"Nearest cylindrical face is {dist:.3f} mm from {_at(d)}.", tol)

    return _result(d, None, f"Unknown kind {k}.", tol, passed=False)  # pragma: no cover


# --------------------------------------------------------------------------
# Feature bookkeeping
# --------------------------------------------------------------------------


def _expected_holes(plan: BuildPlan) -> int:
    n = 0
    for op in plan.operations:
        if isinstance(op, HoleOp):
            per = 2 if op.counterbore_diameter else 1
            n += per * len(hole_positions(op))
    return n


def feature_checks(spec: DimensionSpec, m: Measurer, plan: BuildPlan | None, tol: float) -> list[ValidityCheck]:
    out: list[ValidityCheck] = []
    if plan is not None:
        exp = _expected_holes(plan)
        got = len(m.holes)
        out.append(ValidityCheck(name="hole_count_vs_plan", passed=exp == got,
                                 detail=f"Plan drills {exp} hole surfaces; the model has {got} complete holes."
                                 + ("" if exp == got else " A hole was cut into by another feature, merged, or lost.")))
    # Every hole in the model must be accounted for by the confirmed spec.
    counted_diams = [d.feature_diameter for d in spec.dimensions if d.kind == "hole_count" and d.feature_diameter]
    located = []
    for d in spec.dimensions:
        if d.kind in ("hole_diameter", "hole_depth", "hole_position") and d.at is not None:
            h, _ = m.hole_near(d.at, d.nominal if d.kind == "hole_diameter" else d.feature_diameter)
            if h is not None:
                located.append(h)
    stray = [h for h in m.holes
             if not any(h is x for x in located) and not any(abs(h.diameter - c) <= tol for c in counted_diams)]
    out.append(ValidityCheck(name="no_unconfirmed_holes", passed=not stray,
                             detail="Every hole is covered by a confirmed dimension." if not stray
                             else "Holes not in the confirmed spec: " + "; ".join(h.label() for h in stray)))
    return out


def coverage(spec: DimensionSpec, plan: BuildPlan | None) -> list[CoverageNote]:
    if plan is None:
        return []
    ids = {d.id for d in spec.dimensions}
    notes: list[CoverageNote] = []
    for op in plan.operations:
        if not op.dims:
            notes.append(CoverageNote(feature_id=op.id, message=f"{op.op} '{op.id}' is not tied to any confirmed dimension; check it by eye."))
        unknown = [i for i in op.dims if i not in ids]
        if unknown:
            notes.append(CoverageNote(feature_id=op.id, message=f"{op.id} cites unknown dimension ids: {', '.join(unknown)}."))
    for a in plan.assumptions:
        notes.append(CoverageNote(feature_id="plan", message=f"Assumption: {a}"))
    return notes


def verify(shape, spec: DimensionSpec, plan: BuildPlan | None = None, tol: float = MODEL_TOL_MM) -> VerificationReport:
    m = Measurer(shape)
    validity = validity_checks(shape, m)
    dims = [check_dimension(d, m, tol) for d in spec.dimensions] if m.solids else []
    feats = feature_checks(spec, m, plan, tol) if m.solids else []
    passed = all(v.passed for v in validity) and all(c.passed for c in dims) and all(f.passed for f in feats) and bool(dims)
    return VerificationReport(passed=passed, model_tolerance_mm=tol, validity=validity, dimensions=dims,
                              features=feats, coverage=coverage(spec, plan))
