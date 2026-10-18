"""Builder: every operation produces the volume worked out by hand."""

import math

import pytest

from app.build.builder import BuildError, build
from app.build.schemas import BuildPlan
from app.pipeline.geometry import concave_cylinders
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

BOX = {"kind": "box", "size": {"x": 100, "y": 60, "z": 20}}


def vol(plan: dict) -> float:
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(build(BuildPlan.model_validate(plan)), p)
    return p.Mass()


def hole(d, direction, pts, **kw):
    return {"op": "hole", "id": kw.pop("id", "H1"), "diameter": d, "direction": direction,
            "positions": [{"x": x, "y": y, "z": z} for x, y, z in pts], **kw}


L_PROFILE = [{"u": 0, "v": 0}, {"u": 80, "v": 0}, {"u": 80, "v": 6}, {"u": 6, "v": 6}, {"u": 6, "v": 50}, {"u": 0, "v": 50}]

CASES = {
    "box": ({"stock": BOX}, 120000),
    "box corner radius": ({"stock": {**BOX, "corner_radius": 6}}, 120000 - (4 - math.pi) * 36 * 20),
    "cylinder": ({"stock": {"kind": "cylinder", "diameter": 50, "height": 10}}, math.pi * 625 * 10),
    "L extrusion": ({"stock": {"kind": "extrusion", "plane": "xz", "points": L_PROFILE, "depth": 40}}, (80 * 6 + 6 * 44) * 40),
    "L inside radius": ({"stock": {"kind": "extrusion", "plane": "xz", "points": L_PROFILE, "depth": 40, "concave_corner_radius": 4}},
                        (80 * 6 + 6 * 44 + (1 - math.pi / 4) * 16) * 40),
    "through hole": ({"stock": BOX, "operations": [hole(10, "-z", [(20, 30, 20)])]}, 120000 - math.pi * 25 * 20),
    "blind hole": ({"stock": BOX, "operations": [hole(10, "-z", [(20, 30, 20)], depth=8)]}, 120000 - math.pi * 25 * 8),
    "counterbore": ({"stock": BOX, "operations": [hole(6.6, "-z", [(20, 30, 20)], counterbore_diameter=11, counterbore_depth=6.5)]},
                    120000 - math.pi * 3.3 ** 2 * 13.5 - math.pi * 5.5 ** 2 * 6.5),
    "side hole": ({"stock": BOX, "operations": [hole(8, "+x", [(0, 30, 10)], depth=15)]}, 120000 - math.pi * 16 * 15),
    "bolt circle": ({"stock": {"kind": "cylinder", "diameter": 100, "height": 12}, "operations": [
        {"op": "hole", "id": "H", "diameter": 9, "direction": "-z",
         "bolt_circle": {"center": {"x": 50, "y": 50, "z": 12}, "diameter": 75, "count": 6, "start_angle_deg": 0}}]},
        math.pi * 2500 * 12 - 6 * math.pi * 4.5 ** 2 * 12),
    "pocket": ({"stock": BOX, "operations": [{"op": "pocket", "id": "P", "center": {"x": 50, "y": 30, "z": 20}, "direction": "-z",
                                              "length": 50, "width": 30, "depth": 12, "corner_radius": 5}]},
               120000 - (1500 - (4 - math.pi) * 25) * 12),
    "obround slot": ({"stock": BOX, "operations": [{"op": "pocket", "id": "S", "center": {"x": 50, "y": 30, "z": 20}, "direction": "-z",
                                                    "length": 40, "width": 10, "corner_radius": 5}]},
                     120000 - (300 + math.pi * 25) * 20),
    "round pocket": ({"stock": BOX, "operations": [{"op": "pocket", "id": "S", "center": {"x": 50, "y": 30, "z": 20}, "direction": "-z",
                                                    "length": 20, "width": 20, "depth": 5, "corner_radius": 10}]},
                     120000 - math.pi * 100 * 5),
    "rotated pocket": ({"stock": BOX, "operations": [{"op": "pocket", "id": "P", "center": {"x": 50, "y": 30, "z": 20}, "direction": "-z",
                                                      "length": 30, "width": 10, "depth": 5, "angle_deg": 30}]}, 120000 - 1500),
    "cylinder boss": ({"stock": BOX, "operations": [{"op": "boss", "id": "B", "shape": "cylinder", "base_center": {"x": 50, "y": 30, "z": 20},
                                                     "direction": "+z", "height": 15, "diameter": 30}]}, 120000 + math.pi * 225 * 15),
    "box boss": ({"stock": BOX, "operations": [{"op": "boss", "id": "B", "shape": "box", "base_center": {"x": 50, "y": 30, "z": 20},
                                                "direction": "+z", "height": 5, "length": 20, "width": 10}]}, 121000),
    "step": ({"stock": BOX, "operations": [{"op": "cut_box", "id": "C", "corner_min": {"x": 60, "y": -1, "z": 10},
                                            "corner_max": {"x": 101, "y": 61, "z": 21}}]}, 96000),
    "notch with inside radius": ({"stock": BOX, "operations": [{"op": "cut_box", "id": "C", "corner_min": {"x": 70, "y": 40, "z": -1},
                                                                "corner_max": {"x": 101, "y": 61, "z": 21}, "corner_radius": 3, "corner_axis": "z"}]},
                                 120000 - (600 - (1 - math.pi / 4) * 9) * 20),
}


@pytest.mark.parametrize("name", list(CASES))
def test_volume_matches_hand_calculation(name):
    plan, expected = CASES[name]
    assert vol(plan) == pytest.approx(expected, rel=1e-7)


def test_counterbore_is_two_coaxial_holes():
    shape = build(BuildPlan.model_validate(CASES["counterbore"][0]))
    holes, _ = concave_cylinders(shape)
    assert sorted(round(h.diameter_mm, 3) for h in holes) == [6.6, 11.0]


@pytest.mark.parametrize("bad, message", [
    ({"stock": {**BOX, "corner_radius": 40}}, "corner radius"),
    ({"stock": BOX, "operations": [hole(0, "-z", [(1, 1, 20)])]}, "diameter must be positive"),
    ({"stock": BOX, "operations": [hole(6, "-z", [])]}, "no positions"),
    ({"stock": BOX, "operations": [hole(6, "-z", [(10, 10, 20)], counterbore_diameter=5, counterbore_depth=2)]}, "must exceed"),
    ({"stock": BOX, "operations": [{"op": "pocket", "id": "P", "center": {"x": 50, "y": 30, "z": 20}, "direction": "-z",
                                    "length": 10, "width": 10, "depth": 2, "corner_radius": 6}]}, "exceeds half"),
    ({"stock": {"kind": "extrusion", "plane": "xy", "points": [{"u": 0, "v": 0}, {"u": 1, "v": 0}], "depth": 5}}, "at least 3"),
])
def test_unbuildable_plans_explain_why(bad, message):
    with pytest.raises(BuildError, match=message):
        build(BuildPlan.model_validate(bad))


def test_duplicate_operation_ids_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        BuildPlan.model_validate({"stock": BOX, "operations": [hole(5, "-z", [(10, 10, 20)]), hole(5, "-z", [(30, 10, 20)])]})
