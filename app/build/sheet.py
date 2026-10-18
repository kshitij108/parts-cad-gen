"""2D drawing sheets generated from a solid.

Views are hidden-line projections of the actual model (OpenCascade HLR), and
every number printed comes from measuring the model. The drawing therefore
can't disagree with the STEP file it ships with.

Two modes:
- "deliverable": views, overall dims, measured spec dims, hole table and the
  verification table. Ships with a generated model.
- "input": what a customer drawing looks like (no ids, no probe points, no
  verification). Used to render benchmark inputs; style="sketch" adds a
  hand-drawn wobble.

Rendered to SVG (web preview), PDF (download) and PNG (vision input).
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass, field
from datetime import date

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepLib import BRepLib
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Line
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from app.build.measure import AXIS_INDEX, Measurer
from app.build.schemas import DimensionSpec, RequiredDimension, VerificationReport

_as_edge = getattr(TopoDS, "Edge", None) or TopoDS.Edge_s

A3 = (420.0, 297.0)
VIEWS = {  # view: (towards viewer N, view x-axis); view y = N × x
    "top": ((0, 0, 1), (1, 0, 0)),
    "front": ((0, -1, 0), (1, 0, 0)),
    "right": ((1, 0, 0), (0, 1, 0)),
    "iso": ((1, -1, 1), (1, 1, 0)),
}
VIEW_AXES = {"top": ("x", "y"), "front": ("x", "z"), "right": ("y", "z")}
NORMAL_VIEW = {"z": "top", "y": "front", "x": "right"}
SCALES = [10, 5, 4, 2.5, 2, 1.5, 1, 0.75, 0.5, 0.4, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01]


# --------------------------------------------------------------------------
# Sheet model
# --------------------------------------------------------------------------


@dataclass
class Stroke:
    pts: list[tuple[float, float]]
    kind: str = "visible"  # visible | hidden | thin | dim | border


@dataclass
class Label:
    x: float
    y: float
    text: str
    size: float = 3.5
    anchor: str = "start"  # start | middle | end
    angle: float = 0.0  # degrees, counter-clockwise
    bold: bool = False


@dataclass
class Arrow:
    tip: tuple[float, float]
    direction: tuple[float, float]  # unit vector pointing at the tip
    size: float = 2.5


@dataclass
class Sheet:
    width: float = A3[0]
    height: float = A3[1]
    strokes: list[Stroke] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    arrows: list[Arrow] = field(default_factory=list)
    occupied: list[tuple[float, float, float, float]] = field(default_factory=list)  # boxes labels must avoid

    def free(self, box) -> bool:
        x0, y0, x1, y1 = box
        return not any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in self.occupied)

    def line(self, a, b, kind="thin"):
        self.strokes.append(Stroke([a, b], kind))

    def rect(self, x0, y0, x1, y1, kind="thin"):
        self.strokes.append(Stroke([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], kind))

    def text(self, x, y, s, size=3.5, anchor="start", angle=0.0, bold=False):
        self.labels.append(Label(x, y, s, size, anchor, angle, bold))


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def _polylines(shape, deflection: float) -> list[list[tuple[float, float]]]:
    out = []
    if shape is None or shape.IsNull():
        return out
    BRepLib.BuildCurves3d_s(shape, 1e-6)
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        e = _as_edge(exp.Current())
        exp.Next()
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_Line:
            a, b = c.Value(c.FirstParameter()), c.Value(c.LastParameter())
            out.append([(a.X(), a.Y()), (b.X(), b.Y())])
            continue
        disc = GCPnts_QuasiUniformDeflection(c, deflection)
        if disc.IsDone() and disc.NbPoints() >= 2:
            pts = [disc.Value(i) for i in range(1, disc.NbPoints() + 1)]
            out.append([(p.X(), p.Y()) for p in pts])
    return out


def project(shape, view: str, deflection: float = 0.05) -> dict[str, list[list[tuple[float, float]]]]:
    n, vx = VIEWS[view]
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*n), gp_Dir(*vx))))
    algo.Update()
    algo.Hide()
    hlr = HLRBRep_HLRToShape(algo)
    return {
        "visible": _polylines(hlr.VCompound(), deflection) + _polylines(hlr.OutLineVCompound(), deflection),
        "smooth": _polylines(hlr.Rg1LineVCompound(), deflection),
        "hidden": _polylines(hlr.HCompound(), deflection) + _polylines(hlr.OutLineHCompound(), deflection),
    }


# --------------------------------------------------------------------------
# Layout helpers
# --------------------------------------------------------------------------


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


class ViewPlacer:
    """Maps datum model coordinates of one view into sheet millimetres."""

    def __init__(self, view: str, origin: tuple[float, float], scale: float, m: Measurer):
        self.view, self.origin, self.scale, self.m = view, origin, scale, m
        self.ax = VIEW_AXES.get(view)

    def model(self, p) -> tuple[float, float]:
        """Datum 3D point -> sheet point (orthographic views only)."""
        a, b = self.ax
        return (self.origin[0] + p[AXIS_INDEX[a]] * self.scale, self.origin[1] + p[AXIS_INDEX[b]] * self.scale)

    def proj(self, xy) -> tuple[float, float]:
        """HLR view coordinates (absolute) -> sheet point."""
        a, b = self.ax
        return (self.origin[0] + (xy[0] - self.m.lo[AXIS_INDEX[a]]) * self.scale,
                self.origin[1] + (xy[1] - self.m.lo[AXIS_INDEX[b]]) * self.scale)


def _label_box(cx, cy, text, size, angle):
    w, h = 0.56 * size * len(text) + 0.6, size + 0.6
    if abs(math.sin(math.radians(angle))) > 0.7:
        w, h = h, w
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _dim_line(sh: Sheet, a, b, text: str, size=3.2, ext_from=None):
    """Dimension between sheet points a and b; the value is placed where it
    doesn't collide with other labels or holes."""
    (x0, y0), (x1, y1) = a, b
    L = math.hypot(x1 - x0, y1 - y0)
    if L < 1e-6:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    sh.line(a, b, "dim")
    arrow = min(2.5, L / 3)
    sh.arrows.append(Arrow(a, (-ux, -uy), arrow))
    sh.arrows.append(Arrow(b, (ux, uy), arrow))
    if ext_from:
        for p, q in zip(ext_from, (a, b)):
            sh.line(p, q, "thin")
    ang = math.degrees(math.atan2(uy, ux))
    if ang > 90 or ang <= -90:
        ang += 180
    nx, ny = -math.sin(math.radians(ang)), math.cos(math.radians(ang))
    choice = None
    for t in (0.5, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88):
        for off in (size * 0.5 + 1.0, -(size * 0.5 + 1.0)):
            cx, cy = x0 + (x1 - x0) * t + nx * off, y0 + (y1 - y0) * t + ny * off
            box = _label_box(cx, cy, text, size, ang)
            if sh.free(box):
                choice = (cx, cy, box)
                break
        if choice:
            break
    if choice is None:
        cx, cy = (x0 + x1) / 2 + nx * (size * 0.5 + 1.0), (y0 + y1) / 2 + ny * (size * 0.5 + 1.0)
        choice = (cx, cy, _label_box(cx, cy, text, size, ang))
    cx, cy, box = choice
    sh.occupied.append(box)
    # Baseline sits size*0.35 below the visual centre.
    sh.text(cx - nx * size * 0.35, cy - ny * size * 0.35, text, size, "middle", ang)


def _table(sh: Sheet, x0: float, y_top: float, width: float, rows: list[list[str]], col_frac: list[float],
           title: str, max_height: float) -> float:
    """Draw a simple table downward from y_top; returns the y of its bottom."""
    n = len(rows)
    row_h = min(5.0, max(2.6, (max_height - 6) / max(1, n)))
    size = row_h * 0.62
    sh.text(x0, y_top - 4, title, 3.2, bold=True)
    y = y_top - 6
    xs = [x0]
    for f in col_frac:
        xs.append(xs[-1] + f * width)
    shown = rows if (n * row_h) <= max_height - 6 else rows[: int((max_height - 10) / row_h)]
    for i, r in enumerate(shown):
        sh.line((x0, y), (x0 + width, y), "thin")
        for j, cell in enumerate(r):
            sh.text(xs[j] + 0.8, y - row_h * 0.75, cell, size, bold=(i == 0))
        y -= row_h
    if len(shown) < n:
        sh.text(x0 + 0.8, y - row_h * 0.75, f"... {n - len(shown)} more rows in the package JSON", size)
        y -= row_h
    sh.line((x0, y), (x0 + width, y), "thin")
    for xv in xs:
        sh.line((xv, y_top - 6), (xv, y), "thin")
    return y


# --------------------------------------------------------------------------
# Sheet composition
# --------------------------------------------------------------------------


def make_sheet(shape, spec: DimensionSpec | None = None, report: VerificationReport | None = None, *,
               mode: str = "deliverable", title: str | None = None, material: str | None = None,
               views: tuple[str, ...] = ("top", "front", "right", "iso"), annotate: bool = True,
               chrome: bool = True) -> Sheet:
    """chrome=False drops the border, tables and title block (e.g. a bare
    picture of the part with no size information)."""
    m = Measurer(shape)
    sh = Sheet()
    W, H = sh.width, sh.height
    if chrome:
        sh.rect(8, 8, W - 8, H - 8, "border")

    Lx, Ly, Lz = m.size
    col_x0 = 285.0  # right column: iso, tables, title block
    region = (18.0, 18.0, col_x0 - 10.0, H - 14.0)
    gap, margin = 28.0, 16.0
    avail_w = region[2] - region[0] - margin - 6
    avail_h = region[3] - region[1] - margin - 6
    need_w, need_h = Lx + Ly, Lz + Ly
    fit = min((avail_w - gap) / max(need_w, 1e-6), (avail_h - gap) / max(need_h, 1e-6))
    scale = next((s for s in SCALES if s <= fit), SCALES[-1])
    deflection = max(0.01, 0.2 / scale)

    block_w = (Lx + Ly) * scale + gap + margin
    block_h = (Lz + Ly) * scale + gap + margin
    fx0 = region[0] + margin + max(0.0, (region[2] - region[0] - block_w) / 2)
    fy0 = region[1] + margin + max(0.0, (region[3] - region[1] - block_h) / 2)
    placers = {
        "front": ViewPlacer("front", (fx0, fy0), scale, m),
        "top": ViewPlacer("top", (fx0, fy0 + Lz * scale + gap), scale, m),
        "right": ViewPlacer("right", (fx0 + Lx * scale + gap, fy0), scale, m),
    }
    for v in ("top", "front", "right"):
        if v not in views:
            continue
        pl = placers[v]
        proj = project(shape, v, deflection)
        for kind, polys in (("hidden", proj["hidden"]), ("visible", proj["visible"]), ("thin", proj["smooth"])):
            for poly in polys:
                sh.strokes.append(Stroke([pl.proj(p) for p in poly], kind))
        sh.text(pl.model((0, 0, 0))[0], pl.model(m.size)[1] + 3 if v != "front" else pl.origin[1] + Lz * scale + 3,
                v.upper(), 2.5)

    # Iso view, fitted into the top of the right column (or the whole sheet if alone).
    iso_box = (col_x0, H - 80.0, W - 12.0, H - 14.0) if tuple(views) != ("iso",) else (30.0, 30.0, W - 30.0, H - 30.0)
    if "iso" in views:
        proj = project(shape, "iso", deflection)
        pts = [p for polys in proj.values() for poly in polys for p in poly]
        if pts:
            minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
            miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
            s_iso = min((iso_box[2] - iso_box[0] - 6) / max(maxx - minx, 1e-6), (iso_box[3] - iso_box[1] - 8) / max(maxy - miny, 1e-6))
            ox = (iso_box[0] + iso_box[2]) / 2 - (minx + maxx) / 2 * s_iso
            oy = (iso_box[1] + iso_box[3]) / 2 - (miny + maxy) / 2 * s_iso
            for kind, polys in (("visible", proj["visible"]), ("thin", proj["smooth"])):
                for poly in polys:
                    sh.strokes.append(Stroke([(ox + x * s_iso, oy + y * s_iso) for x, y in poly], kind))
            if chrome:
                sh.text(iso_box[0], iso_box[3] - 2, "ISOMETRIC (not to scale)", 2.5)

    holes = sorted(m.holes, key=lambda h: (h.center[2] * -1, h.center[1], h.center[0], -h.diameter))
    hole_ids = {id(h): f"H{i + 1}" for i, h in enumerate(holes)}

    if annotate and {"top", "front", "right"} <= set(views):
        _annotate(sh, m, placers, spec, report, holes, hole_ids, mode)

    if not chrome:
        return sh

    # Right column tables.
    y = iso_box[1] - 4
    title_top = 52.0
    if holes:
        rows = [["ID", "X", "Y", "Z", "Ø", "DEPTH", "AXIS"]]
        for h in holes:
            depth = "THRU" if m.hole_is_through(h) else _fmt(h.depth)
            axis = "XYZ"[max(range(3), key=lambda i: abs(h.axis[i]))]
            rows.append([hole_ids[id(h)], _fmt(h.center[0]), _fmt(h.center[1]), _fmt(h.center[2]), _fmt(h.diameter), depth, axis])
        space = (y - title_top) * (0.45 if (mode == "deliverable" and spec) else 1.0)
        y = _table(sh, col_x0, y, W - 12 - col_x0, rows, [0.11, 0.15, 0.15, 0.15, 0.15, 0.17, 0.12], "HOLE TABLE (centres from datum)", space) - 4
    if mode == "deliverable" and spec and report:
        rows = [["ID", "DIMENSION", "EXPECTED", "MEASURED", "RESULT"]]
        for c in report.dimensions:
            rows.append([c.dim_id, c.label[:22], c.expected, c.measured or "-", "OK" if c.passed else "FAIL"])
        _table(sh, col_x0, y, W - 12 - col_x0, rows, [0.11, 0.37, 0.18, 0.18, 0.16],
               f"VERIFICATION (model tolerance {report.model_tolerance_mm:g} mm)", y - title_top)

    # Title block.
    tb = (col_x0, 12.0, W - 12.0, 48.0)
    sh.rect(*tb, kind="border")
    name = title or (spec.part_name if spec and spec.part_name else "Part")
    sh.text(tb[0] + 2, tb[3] - 7, name[:40], 5.0, bold=True)
    mat = material or (spec.material_text if spec else None)
    tol = spec.general_tolerance_mm if spec else None
    lines = [
        f"MATERIAL: {mat or 'see job'}",
        f"UNITS: mm    SCALE {('1:' + _fmt(1 / scale)) if scale < 1 else (_fmt(scale) + ':1')}    THIRD-ANGLE PROJECTION",
        f"GENERAL TOLERANCE: ±{_fmt(tol)}" if tol else "GENERAL TOLERANCE: see notes",
        "DATUM: part minimum corner. X right, Y back, Z up.",
    ]
    if mode == "deliverable":
        status = "VERIFIED" if (report and report.passed) else ("NOT VERIFIED" if report else "UNCHECKED")
        lines.append(f"{status} against {len(report.dimensions) if report else 0} confirmed dimensions · {date.today().isoformat()}")
    for i, s in enumerate(lines):
        sh.text(tb[0] + 2, tb[3] - 13 - i * 5, s, 2.8)
    return sh


def _annotate(sh: Sheet, m: Measurer, placers: dict[str, ViewPlacer], spec: DimensionSpec | None,
              report: VerificationReport | None, holes, hole_ids, mode: str) -> None:
    Lx, Ly, Lz = m.size
    front, top, right = placers["front"], placers["top"], placers["right"]
    # Overall dimensions: X under the front view, Z left of it, Y left of the top view.
    a, b = front.model((0, 0, 0)), front.model((Lx, 0, 0))
    _dim_line(sh, (a[0], a[1] - 10), (b[0], b[1] - 10), _fmt(Lx), ext_from=[(a[0], a[1] - 1), (b[0], b[1] - 1)])
    a, b = front.model((0, 0, 0)), front.model((0, 0, Lz))
    _dim_line(sh, (a[0] - 10, a[1]), (b[0] - 10, b[1]), _fmt(Lz), ext_from=[(a[0] - 1, a[1]), (b[0] - 1, b[1])])
    a, b = top.model((0, 0, 0)), top.model((0, Ly, 0))
    _dim_line(sh, (a[0] - 10, a[1]), (b[0] - 10, b[1]), _fmt(Ly), ext_from=[(a[0] - 1, a[1]), (b[0] - 1, b[1])])

    # Holes occupy their outline in the view looking down their axis.
    placed: dict[tuple, int] = {}
    for h in holes:
        axis = "xyz"[max(range(3), key=lambda i: abs(h.axis[i]))]
        pl = placers[NORMAL_VIEW[axis]]
        cx, cy = pl.model(h.center)
        r = h.diameter / 2 * pl.scale
        sh.occupied.append((cx - r, cy - r, cx + r, cy + r))
    # Balloons; holes sharing a centre (counterbores) stack outward.
    for h in holes:
        axis = "xyz"[max(range(3), key=lambda i: abs(h.axis[i]))]
        pl = placers[NORMAL_VIEW[axis]]
        cx, cy = pl.model(h.center)
        key = (NORMAL_VIEW[axis], round(cx, 1), round(cy, 1))
        k = placed.get(key, 0)
        placed[key] = k + 1
        r = max((o.diameter for o in holes if o.center == h.center), default=h.diameter) / 2 * pl.scale
        bx, by = cx + r + 3.5 + 5.5 * k, cy + r + 3.5 + 5.5 * k
        sh.line((cx + h.diameter / 2 * pl.scale * 0.7, cy + h.diameter / 2 * pl.scale * 0.7), (bx - 1.6, by - 1.6), "thin")
        sh.text(bx, by - 1.0, hole_ids[id(h)], 2.6, "middle")
        sh.strokes.append(Stroke(_circle(bx, by, 2.4), "thin"))
        sh.occupied.append((bx - 2.6, by - 2.6, bx + 2.6, by + 2.6))

    if not spec:
        return
    measured = {c.dim_id: c for c in report.dimensions} if report else {}
    for d in spec.dimensions:
        if d.kind in ("overall", "hole_diameter", "hole_depth", "hole_position", "hole_count") or d.at is None:
            continue
        val = measured[d.id].measured_value if d.id in measured and measured[d.id].measured_value is not None else d.nominal
        tag = f"{d.id} " if mode == "deliverable" else ""
        _annotate_dim(sh, m, placers, d, val, tag)


def _circle(cx, cy, r, n=24):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def _annotate_dim(sh: Sheet, m: Measurer, placers, d: RequiredDimension, val: float | None, tag: str) -> None:
    at = (d.at.x, d.at.y, d.at.z)
    if d.kind in ("material_width", "gap_width") and d.axis:
        seg, _ = m.segment_at(d.at, d.axis)
        if seg is None:
            return
        view = "top" if d.axis in ("x", "y") else "front"
        pl = placers[view]
        p0, p1 = list(at), list(at)
        p0[AXIS_INDEX[d.axis]], p1[AXIS_INDEX[d.axis]] = seg.t0, seg.t1
        _dim_line(sh, pl.model(p0), pl.model(p1), tag + (_fmt(val) if val is not None else "?"))
    elif d.kind == "depth" and d.direction:
        axis, s = d.direction[1], (1 if d.direction[0] == "+" else -1)
        view = "front" if axis == "z" else "top"
        pl = placers[view]
        ai = AXIS_INDEX[axis]
        face = 0.0 if s > 0 else m.size[ai]
        p0 = list(at)
        p0[ai] = face
        p1 = list(at)
        p1[ai] = face + s * (val if (val is not None and not d.through) else m.size[ai])
        _dim_line(sh, pl.model(p0), pl.model(p1), tag + ("THRU" if d.through else _fmt(val) if val is not None else "?"))
    elif d.kind == "radius" and d.axis:
        pl = placers[NORMAL_VIEW[d.axis]]
        x, y = pl.model(at)
        text = tag + (d.label if not tag and d.label.startswith(("R", "Ø")) else f"R{_fmt(val)}" if val else "R?")
        for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1), (2, 1), (-2, 1)):
            ex, ey = x + 6 * dx, y + 6 * dy
            box = (ex, ey, ex + 0.56 * 3.0 * len(text) + 1, ey + 3.4) if dx > 0 else (ex - 0.56 * 3.0 * len(text) - 1, ey, ex, ey + 3.4)
            if sh.free(box):
                break
        sh.occupied.append(box)
        n = math.hypot(dx, dy)
        sh.line((x, y), (ex, ey), "thin")
        sh.arrows.append(Arrow((x, y), (-dx / n, -dy / n), 2.0))
        sh.text(ex + (0.5 if dx > 0 else -0.5), ey + 0.4, text, 3.0, "start" if dx > 0 else "end")


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

STROKE_W = {"visible": 0.5, "hidden": 0.25, "thin": 0.18, "dim": 0.18, "border": 0.7}
DASH = {"hidden": (2.0, 1.2)}


def to_svg(sh: Sheet) -> str:
    W, H = sh.width, sh.height
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:g} {H:g}" width="100%" '
           f'style="background:#fff" font-family="Helvetica, Arial, sans-serif">']
    for s in sh.strokes:
        pts = " ".join(f"{x:.2f},{H - y:.2f}" for x, y in s.pts)
        dash = f' stroke-dasharray="{DASH[s.kind][0]},{DASH[s.kind][1]}"' if s.kind in DASH else ""
        color = "#555" if s.kind == "hidden" else "#111"
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{STROKE_W[s.kind]}"{dash} '
                   f'stroke-linecap="round" stroke-linejoin="round"/>')
    for a in sh.arrows:
        tri = _arrow_tri(a)
        out.append('<polygon points="' + " ".join(f"{x:.2f},{H - y:.2f}" for x, y in tri) + '" fill="#111"/>')
    for t in sh.labels:
        anchor = {"start": "start", "middle": "middle", "end": "end"}[t.anchor]
        x, y = t.x, H - t.y
        rot = f' transform="rotate({-t.angle:.1f} {x:.2f} {y:.2f})"' if t.angle else ""
        weight = ' font-weight="bold"' if t.bold else ""
        txt = t.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{t.size:.2f}" text-anchor="{anchor}"{weight}{rot} fill="#111">{txt}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _arrow_tri(a: Arrow):
    (tx, ty), (dx, dy) = a.tip, a.direction
    bx, by = tx - dx * a.size, ty - dy * a.size
    w = a.size * 0.3
    return [(tx, ty), (bx - dy * w, by + dx * w), (bx + dy * w, by - dx * w)]


def _pdf_text(s: str) -> str:
    b = s.encode("cp1252", errors="replace").decode("latin-1")
    return b.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def to_pdf(sh: Sheet) -> bytes:
    k = 72.0 / 25.4
    ops: list[str] = ["1 J 1 j"]
    for s in sh.strokes:
        ops.append(f"{STROKE_W[s.kind] * k:.3f} w")
        ops.append(f"[{DASH[s.kind][0] * k:.2f} {DASH[s.kind][1] * k:.2f}] 0 d" if s.kind in DASH else "[] 0 d")
        ops.append("0.33 G" if s.kind == "hidden" else "0 G")
        x0, y0 = s.pts[0]
        ops.append(f"{x0 * k:.2f} {y0 * k:.2f} m " + " ".join(f"{x * k:.2f} {y * k:.2f} l" for x, y in s.pts[1:]) + " S")
    ops.append("[] 0 d 0 g")
    for a in sh.arrows:
        tri = _arrow_tri(a)
        ops.append(f"{tri[0][0] * k:.2f} {tri[0][1] * k:.2f} m {tri[1][0] * k:.2f} {tri[1][1] * k:.2f} l {tri[2][0] * k:.2f} {tri[2][1] * k:.2f} l f")
    for t in sh.labels:
        size = t.size * k
        width = 0.52 * size * len(t.text)
        shift = {"start": 0.0, "middle": width / 2, "end": width}[t.anchor]
        c, s_ = math.cos(math.radians(t.angle)), math.sin(math.radians(t.angle))
        x = t.x * k - shift * c
        y = t.y * k - shift * s_
        font = "/F2" if t.bold else "/F1"
        ops.append(f"BT {font} {size:.2f} Tf {c:.4f} {s_:.4f} {-s_:.4f} {c:.4f} {x:.2f} {y:.2f} Tm ({_pdf_text(t.text)}) Tj ET")
    content = "\n".join(ops).encode("latin-1")
    w, h = sh.width * k, sh.height * k
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return buf.getvalue()


_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(px: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def to_png(sh: Sheet, dpi: int = 150, style: str = "clean", seed: int = 0) -> bytes:
    """Raster render. style='sketch' wobbles strokes like a hand drawing."""
    from PIL import Image, ImageDraw

    k = dpi / 25.4
    W, H = int(sh.width * k), int(sh.height * k)
    img = Image.new("RGB", (W, H), (255, 255, 255) if style == "clean" else (250, 248, 240))
    dr = ImageDraw.Draw(img)
    rng = random.Random(seed)

    def P(x, y):
        return (x * k, (sh.height - y) * k)

    def wobble(pts):
        if style != "sketch":
            return pts
        out = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            n = max(2, int(L / 6))
            nx, ny = (-(y1 - y0) / L, (x1 - x0) / L) if L > 0 else (0, 0)
            for i in range(n + 1):
                t = i / n
                j = rng.uniform(-0.35, 0.35) * min(1.0, L / 4)
                out.append((x0 + (x1 - x0) * t + nx * j, y0 + (y1 - y0) * t + ny * j))
        return out

    for s in sh.strokes:
        if style == "sketch" and s.kind == "border":
            continue
        width = max(1, int(round(STROKE_W[s.kind] * k * (1.4 if style == "sketch" else 1))))
        color = (95, 95, 95) if s.kind == "hidden" else (20, 20, 30)
        pts = wobble(s.pts)
        if s.kind in DASH:
            on, off = DASH[s.kind]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                L = math.hypot(x1 - x0, y1 - y0)
                t = 0.0
                while t < L:
                    t1 = min(L, t + on)
                    dr.line([P(x0 + (x1 - x0) * t / L, y0 + (y1 - y0) * t / L), P(x0 + (x1 - x0) * t1 / L, y0 + (y1 - y0) * t1 / L)],
                            fill=color, width=width)
                    t = t1 + off
        else:
            dr.line([P(x, y) for x, y in pts], fill=color, width=width, joint="curve")
    for a in sh.arrows:
        dr.polygon([P(x, y) for x, y in _arrow_tri(a)], fill=(20, 20, 30))
    for t in sh.labels:
        font = _font(max(8, int(t.size * k)))
        anchor = {"start": "ls", "middle": "ms", "end": "rs"}[t.anchor]
        if not t.angle:
            dr.text(P(t.x, t.y), t.text, fill=(15, 15, 25), font=font, anchor=anchor)
            continue
        tw = int(font.getlength(t.text)) + 8
        th = int(t.size * k * 1.6) + 6
        tile = Image.new("RGBA", (tw * 2, th * 2), (255, 255, 255, 0))
        ImageDraw.Draw(tile).text((tw, th), t.text, fill=(15, 15, 25, 255), font=font, anchor=anchor)
        rot = tile.rotate(t.angle, center=(tw, th), resample=Image.BICUBIC)
        px, py = P(t.x, t.y)
        img.paste(rot, (int(px - tw), int(py - th)), rot)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
