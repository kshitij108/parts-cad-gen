"""Reference parts for the build benchmark.

Each case is a known-good plan. The benchmark builds it, derives the ground
truth dimensions from it, renders an input (drawing, sketch or bare picture)
and asks the agent to rebuild the part from that input alone. Extras cover
what derive_spec can't infer (profile thicknesses, step locations, bosses).

Probe points in extras are chosen to avoid other features; derive_spec raises
if any extra doesn't hold on the reference model, so a bad case fails loudly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from app.build.schemas import BuildPlan, RequiredDimension
from app.schemas import Vec3


def _dim(kind, nominal=None, label=None, **kw) -> RequiredDimension:
    at = kw.pop("at", None)
    return RequiredDimension(id="X", label=label or (f"{nominal:g}" if nominal is not None else kind), kind=kind,
                             nominal=nominal, at=Vec3(x=at[0], y=at[1], z=at[2]) if at else None,
                             tol_plus=0.1, tol_minus=0.1, **kw)


@dataclass
class BenchCase:
    name: str
    title: str
    tests: str  # what this case exercises
    plan: dict
    extras: list[RequiredDimension] = field(default_factory=list)
    material: str | None = "Aluminium 6061-T6"
    expect: Literal["model", "refusal"] = "model"
    picture_only: bool = False  # render a bare isometric picture with no sizes

    def build_plan(self) -> BuildPlan:
        return BuildPlan.model_validate(self.plan)


def _holes(diameter, direction, pts, **kw):
    return {"op": "hole", "id": kw.pop("id", "H1"), "diameter": diameter, "direction": direction,
            "positions": [{"x": x, "y": y, "z": z} for x, y, z in pts], **kw}


CASES: list[BenchCase] = [
    BenchCase(
        name="plate_4_holes", title="Base plate",
        tests="Simplest case: plate with a 4X through-hole pattern.",
        plan={"stock": {"kind": "box", "size": {"x": 120, "y": 80, "z": 10}},
              "operations": [_holes(8.5, "-z", [(10, 10, 10), (110, 10, 10), (10, 70, 10), (110, 70, 10)])]},
    ),
    BenchCase(
        name="block_pocket", title="Pocket block",
        tests="Rounded pocket located from edges, counterbored hole on the pocket floor, corner holes.",
        plan={"stock": {"kind": "box", "size": {"x": 100, "y": 60, "z": 25}},
              "operations": [
                  {"op": "pocket", "id": "P1", "center": {"x": 50, "y": 30, "z": 25}, "direction": "-z",
                   "length": 50, "width": 30, "depth": 12, "corner_radius": 5},
                  _holes(8.5, "-z", [(10, 10, 25), (90, 10, 25), (10, 50, 25), (90, 50, 25)]),
                  _holes(6.6, "-z", [(50, 30, 13)], id="H2", counterbore_diameter=11, counterbore_depth=4)]},
    ),
    BenchCase(
        name="flange_bolt_circle", title="Flange",
        tests="Round stock, central bore, 6X holes on a pitch circle (positions need trigonometry).",
        plan={"stock": {"kind": "cylinder", "diameter": 100, "height": 12},
              "operations": [
                  _holes(40, "-z", [(50, 50, 12)], id="H1"),
                  {"op": "hole", "id": "H2", "diameter": 9, "direction": "-z",
                   "bolt_circle": {"center": {"x": 50, "y": 50, "z": 12}, "diameter": 75, "count": 6, "start_angle_deg": 0}}]},
    ),
    BenchCase(
        name="l_bracket", title="L bracket",
        tests="Extruded L profile with an inside bend radius; holes drilled in two directions.",
        plan={"stock": {"kind": "extrusion", "plane": "xz", "depth": 40, "concave_corner_radius": 4,
                        "points": [{"u": 0, "v": 0}, {"u": 80, "v": 0}, {"u": 80, "v": 6}, {"u": 6, "v": 6},
                                   {"u": 6, "v": 50}, {"u": 0, "v": 50}]},
              "operations": [
                  _holes(6.6, "-z", [(50, 10, 6), (50, 30, 6)], id="H1"),
                  _holes(6.6, "+x", [(0, 10, 32), (0, 30, 32)], id="H2")]},
        extras=[
            _dim("material_width", 6, "6 (base)", at=(65, 20, 3), axis="z"),
            _dim("material_width", 6, "6 (leg)", at=(3, 20, 42), axis="x"),
            _dim("radius", 4, "R4", at=(10 - 4 / math.sqrt(2), 20, 10 - 4 / math.sqrt(2)), axis="y"),
        ],
    ),
    BenchCase(
        name="stepped_block", title="Stepped block",
        tests="Step cut (cut_box) located from an edge, blind holes on two levels.",
        plan={"stock": {"kind": "box", "size": {"x": 80, "y": 50, "z": 30}},
              "operations": [
                  {"op": "cut_box", "id": "C1", "corner_min": {"x": 50, "y": -1, "z": 15}, "corner_max": {"x": 81, "y": 51, "z": 31}},
                  _holes(6, "-z", [(25, 25, 30)], id="H1", depth=10),
                  _holes(5, "-z", [(65, 25, 15)], id="H2", depth=8)]},
        extras=[
            _dim("depth", 15, "15 (step)", at=(65, 10, 30), direction="-z"),
            _dim("material_width", 50, "50 (step at)", at=(25, 10, 22.5), axis="x"),
        ],
    ),
    BenchCase(
        name="slotted_cbore_plate", title="Slotted plate",
        tests="Through obround slot plus counterbored holes; full-round slot ends.",
        plan={"stock": {"kind": "box", "size": {"x": 90, "y": 60, "z": 15}},
              "operations": [
                  {"op": "pocket", "id": "S1", "center": {"x": 45, "y": 30, "z": 15}, "direction": "-z",
                   "length": 40, "width": 10, "depth": None, "corner_radius": 5},
                  _holes(6.6, "-z", [(15, 15, 15), (75, 45, 15)], id="H1", counterbore_diameter=11, counterbore_depth=6.5)]},
    ),
    BenchCase(
        name="boss_plate", title="Boss plate",
        tests="Cylindrical boss on a plate with a bore through both; height read from the side view.",
        plan={"stock": {"kind": "box", "size": {"x": 80, "y": 80, "z": 10}},
              "operations": [
                  {"op": "boss", "id": "B1", "shape": "cylinder", "base_center": {"x": 40, "y": 40, "z": 10},
                   "direction": "+z", "height": 15, "diameter": 30},
                  _holes(12, "-z", [(40, 40, 25)], id="H1"),
                  _holes(6.6, "-z", [(10, 10, 10), (70, 10, 10), (10, 70, 10), (70, 70, 10)], id="H2")]},
        extras=[
            _dim("depth", 15, "15 (boss height)", at=(20, 40, 25), direction="-z"),
            _dim("radius", 15, "Ø30 boss", at=(55, 40, 17.5), axis="z"),
            _dim("material_width", 10, "10 (plate)", at=(20, 25, 5), axis="z"),
        ],
    ),
    BenchCase(
        name="side_holes_block", title="Side-drilled block",
        tests="Holes along X and Y (read from the front and right views) plus a top pocket.",
        plan={"stock": {"kind": "box", "size": {"x": 60, "y": 40, "z": 30}},
              "operations": [
                  {"op": "pocket", "id": "P1", "center": {"x": 30, "y": 20, "z": 30}, "direction": "-z",
                   "length": 30, "width": 20, "depth": 8, "corner_radius": 3},
                  _holes(5, "-x", [(60, 20, 15)], id="H1", depth=12),
                  _holes(8, "+y", [(30, 0, 15)], id="H2")]},
    ),
    BenchCase(
        name="photo_no_scale", title="Unlabelled photo",
        tests="A picture with no dimensions or reference object: the agent must refuse to invent sizes.",
        plan={"stock": {"kind": "box", "size": {"x": 120, "y": 80, "z": 10}},
              "operations": [_holes(8.5, "-z", [(10, 10, 10), (110, 10, 10), (10, 70, 10), (110, 70, 10)])]},
        expect="refusal", picture_only=True,
    ),
]

BY_NAME = {c.name: c for c in CASES}


def _ops(plan, kind):
    return [o for o in plan["operations"] if o["op"] == kind]


def _m_hole_move(plan):
    for op in _ops(plan, "hole"):
        if op.get("positions"):
            ax = {"-z": "x", "+z": "x", "-x": "y", "+x": "y", "-y": "x", "+y": "x"}[op["direction"]]
            op["positions"][0][ax] += 0.5
            return f"{op['id']}: first hole moved +0.5 mm along {ax.upper()}"


def _m_hole_diameter(plan):
    for op in _ops(plan, "hole"):
        op["diameter"] -= 0.2
        return f"{op['id']}: hole diameter -0.2 mm"


def _m_hole_depth(plan):
    for op in _ops(plan, "hole"):
        if op.get("depth"):
            op["depth"] += 0.5
            return f"{op['id']}: blind hole 0.5 mm deeper"


def _m_hole_through(plan):
    for op in _ops(plan, "hole"):
        if op.get("depth") is None and not op.get("counterbore_diameter"):
            op["depth"] = 3.0
            return f"{op['id']}: through hole made 3 mm blind"


def _m_bolt_circle(plan):
    for op in _ops(plan, "hole"):
        if op.get("bolt_circle"):
            op["bolt_circle"]["diameter"] += 1
            return f"{op['id']}: pitch circle +1 mm"


def _m_counterbore(plan):
    for op in _ops(plan, "hole"):
        if op.get("counterbore_depth"):
            op["counterbore_depth"] += 0.5
            return f"{op['id']}: counterbore 0.5 mm deeper"


def _m_pocket_depth(plan):
    for op in _ops(plan, "pocket"):
        if op.get("depth"):
            op["depth"] += 0.5
            return f"{op['id']}: pocket 0.5 mm deeper"


def _m_pocket_size(plan):
    for op in _ops(plan, "pocket"):
        op["length"] += 1
        return f"{op['id']}: pocket 1 mm longer"


def _m_pocket_radius(plan):
    for op in _ops(plan, "pocket"):
        r = op.get("corner_radius") or 0
        if 1.5 < r < min(op["length"], op["width"]) / 2:
            op["corner_radius"] = r - 1
            return f"{op['id']}: corner radius -1 mm"


def _m_boss_height(plan):
    for op in _ops(plan, "boss"):
        op["height"] += 0.5
        return f"{op['id']}: boss 0.5 mm taller"


def _m_cut_depth(plan):
    for op in _ops(plan, "cut_box"):
        op["corner_min"]["z"] -= 0.5
        return f"{op['id']}: step cut 0.5 mm deeper"


def _m_stock(plan):
    st = plan["stock"]
    if st["kind"] == "box":
        st["size"]["x"] += 0.5
    elif st["kind"] == "cylinder":
        st["diameter"] += 0.5
    else:
        st["points"][2]["v"] += 0.5
        return "profile corner moved 0.5 mm"
    return "stock 0.5 mm larger"


MUTATIONS = {
    "hole_move": _m_hole_move, "hole_diameter": _m_hole_diameter, "hole_depth": _m_hole_depth,
    "hole_through": _m_hole_through, "bolt_circle": _m_bolt_circle, "counterbore": _m_counterbore,
    "pocket_depth": _m_pocket_depth, "pocket_size": _m_pocket_size, "pocket_radius": _m_pocket_radius,
    "boss_height": _m_boss_height, "cut_depth": _m_cut_depth, "stock": _m_stock,
}


def applicable_mutations(plan: dict) -> list[str]:
    import copy

    return [k for k, fn in MUTATIONS.items() if fn(copy.deepcopy(plan)) is not None]


def mutator(case_name: str):
    """Pick one applicable mutation per case, varied by case name."""
    import zlib

    def mutate(plan: dict) -> str:
        kinds = applicable_mutations(plan)
        k = kinds[zlib.crc32(case_name.encode()) % len(kinds)]
        return f"[{k}] " + MUTATIONS[k](plan)

    return mutate
