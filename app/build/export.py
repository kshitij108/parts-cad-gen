"""Write a built solid out as STEP, drawing (PDF + SVG) and STL preview mesh."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer

from app.build.measure import Measurer
from app.build.schemas import DimensionSpec, GeneratedFile, VerificationReport
from app.build.sheet import make_sheet, to_pdf, to_svg
from app.build.verify import write_step


def _record(name: str, path: Path) -> GeneratedFile:
    data = path.read_bytes()
    os.chmod(path, 0o600)
    return GeneratedFile(name=name, path=str(path), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def write_stl(shape, path: Path) -> Path:
    m = Measurer(shape)
    defl = max(0.01, math.dist((0, 0, 0), m.size) / 1500)
    BRepMesh_IncrementalMesh(shape, defl, False, 0.3, True)
    w = StlAPI_Writer()
    w.ASCIIMode = False
    if not w.Write(shape, str(path)):
        raise IOError(f"could not write {path}")
    return path


def export_attempt(shape, out_dir: Path, spec: DimensionSpec, report: VerificationReport,
                   title: str | None = None, material: str | None = None) -> dict[str, GeneratedFile]:
    """Verified models get STEP + drawing; failed ones only a preview mesh for debugging."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"stl": _record("preview.stl", write_stl(shape, out_dir / "preview.stl"))}
    if report.passed:
        files["step"] = _record("part.step", write_step(shape, out_dir / "part.step"))
        sheet = make_sheet(shape, spec, report, mode="deliverable", title=title, material=material)
        (out_dir / "drawing.pdf").write_bytes(to_pdf(sheet))
        (out_dir / "drawing.svg").write_text(to_svg(sheet))
        files["pdf"] = _record("drawing.pdf", out_dir / "drawing.pdf")
        files["svg"] = _record("drawing.svg", out_dir / "drawing.svg")
    return files
