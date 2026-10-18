"""Verification: the reference model passes, and every deliberate error is caught."""

import copy

import pytest

from app.build.bench.cases import CASES, MUTATIONS, applicable_mutations
from app.build.builder import build
from app.build.derive import DerivationError, derive_spec
from app.build.measure import Measurer
from app.build.schemas import BuildPlan, DimensionSpec, RequiredDimension, readiness
from app.build.verify import check_dimension, verify
from app.schemas import OpenQuestion, Vec3

MODEL_CASES = [c for c in CASES if c.expect == "model"]


@pytest.fixture(scope="module")
def references():
    out = {}
    for c in MODEL_CASES:
        plan = c.build_plan()
        shape = build(plan)
        spec, dropped = derive_spec(plan, shape, extras=c.extras)
        out[c.name] = (plan, shape, spec, dropped)
    return out


@pytest.mark.parametrize("case", MODEL_CASES, ids=lambda c: c.name)
def test_reference_model_passes_its_own_ground_truth(case, references):
    plan, shape, spec, dropped = references[case.name]
    assert dropped == []
    report = verify(shape, spec, plan)
    assert report.passed, report.failures()
    assert all(v.passed for v in report.validity)


MUTANTS = [(c, k) for c in MODEL_CASES for k in applicable_mutations(c.plan)]


@pytest.mark.parametrize("case, kind", MUTANTS, ids=[f"{c.name}-{k}" for c, k in MUTANTS])
def test_every_deliberate_error_is_caught(case, kind, references):
    _, _, spec, _ = references[case.name]
    p = copy.deepcopy(case.plan)
    MUTATIONS[kind](p)
    plan = BuildPlan.model_validate(p)
    report = verify(build(plan), spec, plan)
    assert not report.passed, f"{kind} escaped verification"


def test_stray_hole_is_flagged(references):
    plan, _, spec, _ = references["block_pocket"]
    p = plan.model_dump()
    p["operations"].append({"op": "hole", "id": "HX", "diameter": 3, "direction": "-z", "positions": [{"x": 50, "y": 5, "z": 25}]})
    plan2 = BuildPlan.model_validate(p)
    report = verify(build(plan2), spec, plan2)
    stray = next(f for f in report.features if f.name == "no_unconfirmed_holes")
    assert not stray.passed and "Ø3.000" in stray.detail


def test_counterbore_reads_as_blind_and_hole_as_through(references):
    _, shape, _, _ = references["block_pocket"]
    m = Measurer(shape)
    cb, _ = m.hole_near(Vec3(x=50, y=30, z=13), 11)
    thru, _ = m.hole_near(Vec3(x=50, y=30, z=13), 6.6)
    assert not m.hole_is_through(cb) and m.hole_is_through(thru)


def _dim(**kw):
    return RequiredDimension(id="D1", label="x", **kw)


def test_probe_point_errors_are_explained(references):
    _, shape, _, _ = references["block_pocket"]
    m = Measurer(shape)
    in_air = check_dimension(_dim(kind="material_width", nominal=10, at=Vec3(x=50, y=30, z=20), axis="x"), m)
    assert not in_air.passed and "empty space" in in_air.method
    open_gap = check_dimension(_dim(kind="gap_width", nominal=10, at=Vec3(x=50, y=30, z=30), axis="x"), m)
    assert not open_gap.passed and ("open" in open_gap.method or "outside" in open_gap.method)
    no_hole = check_dimension(_dim(kind="hole_diameter", nominal=8.5, at=Vec3(x=30, y=30, z=25)), m)
    assert not no_hole.passed and "No hole" in no_hole.method


def test_readiness_gate_blocks_generation_without_scale_or_sizes():
    spec = DimensionSpec(scale_basis="none", open_questions=[OpenQuestion(id="Q1", field="dimensions", question="Size?")])
    issues = readiness(spec)
    assert any("No scale" in i for i in issues)
    assert sum("Overall size" in i for i in issues) == 3
    assert any("Size?" in i for i in issues)


def test_bad_extra_dimension_fails_loudly():
    case = next(c for c in CASES if c.name == "plate_4_holes")
    plan = case.build_plan()
    with pytest.raises(DerivationError):
        derive_spec(plan, build(plan), extras=[RequiredDimension(id="X", label="bogus", kind="material_width", nominal=99,
                                                                 at=Vec3(x=60, y=40, z=5), axis="z")])
