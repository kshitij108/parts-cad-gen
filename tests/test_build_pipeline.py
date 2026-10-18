"""Generate loop: retries with measured feedback, stops on API failure, gates on readiness."""

import pytest

from app.build.agent import LLMError, StaticPlanner
from app.build.bench.cases import BY_NAME
from app.build.builder import build
from app.build.derive import derive_spec
from app.build.fixes import fix_internal_corners
from app.build.pipeline import NotReady, generate
from app.build.schemas import BuildPlan, DimensionSpec
from app.build.verify import verify
from app.schemas import RateCard
import json
from pathlib import Path


@pytest.fixture(scope="module")
def ref():
    case = BY_NAME["block_pocket"]
    plan = case.build_plan()
    spec, _ = derive_spec(plan, build(plan))
    return plan, spec


class ScriptedPlanner:
    """Wrong first (hole off by 0.5 mm), right second."""
    source = "planner"

    def __init__(self, good: BuildPlan):
        self.good, self.seen = good, []

    def plan(self, spec, inputs, previous):
        self.seen.append(previous)
        if previous is None:
            bad = self.good.model_dump()
            bad["operations"][1]["positions"][0]["x"] += 0.5
            return BuildPlan.model_validate(bad), {"input_tokens": 1, "output_tokens": 1}
        return self.good, None


def test_retry_uses_measured_failures(ref, tmp_path):
    plan, spec = ref
    planner = ScriptedPlanner(plan)
    recs = generate(spec, [], planner, tmp_path)
    assert [r.passed for r in recs] == [False, True]
    assert any("expected 10.000, measured 10.500" in f for f in recs[1].feedback_used)
    assert set(recs[1].files) == {"stl", "step", "pdf", "svg"}
    assert set(recs[0].files) == {"stl"}  # failed models get a preview only


def test_api_failure_stops_the_loop(ref, tmp_path):
    class Down:
        source = "planner"

        def plan(self, *a):
            raise LLMError("Could not reach the Anthropic API.")

    recs = generate(ref[1], [], Down(), tmp_path, max_attempts=3)
    assert len(recs) == 1 and "unavailable" in recs[0].plan_error


def test_not_ready_spec_is_refused(tmp_path):
    with pytest.raises(NotReady):
        generate(DimensionSpec(scale_basis="none"), [], StaticPlanner(BuildPlan.model_validate(
            {"stock": {"kind": "box", "size": {"x": 1, "y": 1, "z": 1}}})), tmp_path)


def test_internal_corner_fix_adds_radius_and_still_verifies(tmp_path):
    rc = RateCard.model_validate(json.loads((Path(__file__).parent.parent / "app" / "example_rate_card.json").read_text()))
    plan = BuildPlan.model_validate({"stock": {"kind": "box", "size": {"x": 80, "y": 60, "z": 20}}, "operations": [
        {"op": "pocket", "id": "P1", "center": {"x": 40, "y": 30, "z": 20}, "direction": "-z", "length": 40, "width": 30, "depth": 9}]})
    spec, _ = derive_spec(plan, build(plan))
    fixed, spec2, changes = fix_internal_corners(plan, spec, rc)
    assert fixed.operations[0].corner_radius == 3.0  # depth 9 / 3
    assert "P1: corner radius set to R3 (was sharp)." in changes[0]
    new = spec2.dimensions[-1]
    assert new.kind == "radius" and new.source == "dfm_fix" and new.id in fixed.operations[0].dims
    shape = build(fixed)
    report = verify(shape, spec2, fixed)
    # The old pocket-size dims still hold; the new radius is measured.
    assert report.passed, report.failures()
