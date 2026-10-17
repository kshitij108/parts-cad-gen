"""Build small STEP parts with OpenCascade so tests don't depend on binary fixtures."""

from __future__ import annotations

from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt


def _cut(a, b):
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def bracket_shape():
    """100 x 60 x 20 block, Ø10 through hole, 40 x 20 x 10 sharp-cornered pocket."""
    block = BRepPrimAPI_MakeBox(100.0, 60.0, 20.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 30, -1), gp_Dir(0, 0, 1)), 5.0, 22.0).Shape()
    pocket = BRepPrimAPI_MakeBox(gp_Pnt(50, 20, 10), 40.0, 20.0, 11.0).Shape()
    return _cut(_cut(block, hole), pocket)


def thin_wall_shape():
    """50 x 50 x 30 box hollowed from the top leaving 0.6 mm side walls."""
    outer = BRepPrimAPI_MakeBox(50.0, 50.0, 30.0).Shape()
    inner = BRepPrimAPI_MakeBox(gp_Pnt(0.6, 0.6, 3.0), 48.8, 48.8, 28.0).Shape()
    return _cut(outer, inner)


def write_step(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(str(path)) == IFSelect_RetDone
    return path
