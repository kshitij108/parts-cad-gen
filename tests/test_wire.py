"""The planner's flat wire format: explicit through flag, and ambiguous answers rejected (audit E5)."""

import json

import pytest

from app.build.agent import output_schema
from app.build.bench.cases import CASES
from app.build.wire import PlanInvalid, WirePlan, from_plan, to_plan

STOCK = dict(kind="box", size_x=60, size_y=40, size_z=20, corner_radius=0, diameter=0, height=0, plane="none",
             points=[], depth=0, concave_corner_radius=0, convex_corner_radius=0)
HOLE = dict(op="hole", id="H1", dims=[], direction="-z", positions=[[10, 10, 20]], bolt_circle=[], diameter=6, depth=0,
            through=True, counterbore_diameter=0, counterbore_depth=0, center=[], length=0, width=0, corner_radius=0,
            angle_deg=0, shape="none", height=0, corner_min=[], corner_max=[], corner_axis="none")
POCKET = dict(HOLE, op="pocket", id="P1", positions=[], diameter=0, center=[30, 20, 20], length=20, width=10, depth=4, through=False)
CUT = dict(HOLE, op="cut_box", id="C1", direction="none", positions=[], diameter=0, through=False,
           corner_min=[20, 10, 10], corner_max=[40, 30, 21], corner_radius=3, corner_axis="z")


def plan(*ops):
    return to_plan(WirePlan.model_validate({"stock": STOCK, "operations": list(ops), "assumptions": []}))


def test_through_flag_maps_to_through_all_and_blind_depth():
    assert plan(HOLE).operations[0].depth is None
    assert plan(dict(HOLE, through=False, depth=8)).operations[0].depth == 8
    assert plan(dict(POCKET, through=True, depth=0)).operations[0].depth is None
    assert plan(POCKET).operations[0].depth == 4


@pytest.mark.parametrize("op, message", [
    (dict(HOLE, through=False, depth=0), "needs a positive depth"),
    (dict(HOLE, through=True, depth=5), "through=true but depth"),
    (dict(POCKET, through=False, depth=0), "needs a positive depth"),
    (dict(HOLE, bolt_circle=[30, 20, 20, 20, 4, 0]), "not both"),
    (dict(HOLE, positions=[]), "needs positions or a bolt_circle"),
    (dict(CUT, corner_axis="none"), "corner_radius needs corner_axis"),
    (dict(CUT, through=True), "through only applies"),
])
def test_ambiguous_answers_are_rejected_with_a_reason(op, message):
    with pytest.raises(PlanInvalid, match=message):
        plan(op)


def test_round_trip_keeps_every_reference_plan_and_sets_through():
    for c in CASES:
        p = c.build_plan()
        w = from_plan(p)
        assert to_plan(w) == p, c.name
        for wop, op in zip(w.operations, p.operations):
            if op.op in ("hole", "pocket"):
                assert wop.through == (op.depth is None)
            else:
                assert wop.through is False


def test_stock_origin_round_trips_and_is_refused_for_extrusions():
    w = WirePlan.model_validate({"stock": dict(STOCK, origin=[0, 0, 15]), "operations": [], "assumptions": []})
    p = to_plan(w)
    assert (p.stock.origin.x, p.stock.origin.y, p.stock.origin.z) == (0, 0, 15)
    assert from_plan(p).stock.origin == [0, 0, 15]
    assert to_plan(WirePlan.model_validate({"stock": dict(STOCK, origin=[]), "operations": [], "assumptions": []})).stock.origin.z == 0
    ext = dict(STOCK, kind="extrusion", plane="xy", points=[[0, 0], [10, 0], [10, 10]], depth=5, origin=[1, 0, 0])
    with pytest.raises(PlanInvalid, match="extrusion points are already absolute"):
        to_plan(WirePlan.model_validate({"stock": ext, "operations": [], "assumptions": []}))


def test_wire_schema_stays_within_structured_output_limits():
    s = json.dumps(output_schema(WirePlan))
    assert '"anyOf"' not in s  # no union-typed fields at all (limit is 16)
    assert '"through"' in s
