"""Claude client wrapper, exercised against a fake API client (no network)."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.build.agent import (
    ClaudeExtractor,
    ClaudeJSON,
    ClaudePlanner,
    LLMError,
    PlanInvalid,
    input_blocks,
    output_schema,
)
from app.build.bench.cases import CASES
from app.build.schemas import AttemptRecord, BuildPlan, DimensionSpec, VerificationReport, CheckResult
from app.build.wire import WirePlan, from_plan, to_plan


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, text=None, stop_reason="end_turn", category=None):
        self.calls = []
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=900)
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)] if text is not None else []
        self.msg = SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, model="claude-opus-5-5",
                                   stop_details=SimpleNamespace(category=category))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.msg)


PLAN = {"stock": {"kind": "box", "size": {"x": 10, "y": 10, "z": 5}, "corner_radius": None}, "operations": [], "assumptions": []}


def test_request_shape():
    fake = FakeClient(json.dumps(PLAN))
    plan, usage = ClaudeJSON(client=fake).call("system", [{"type": "text", "text": "hi"}], BuildPlan)
    assert plan.stock.size.x == 10 and usage["input_tokens"] == 1200
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("stop, match", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_refusal_and_truncation_raise(stop, match):
    with pytest.raises(LLMError, match=match):
        ClaudeJSON(client=FakeClient("{}", stop_reason=stop, category="cyber")).call("s", [], BuildPlan)


def test_invalid_plan_is_retryable_not_fatal():
    bad = {**PLAN, "operations": [{"op": "hole", "id": "H", "dims": [], "diameter": 5, "direction": "-z", "positions": [],
                                   "bolt_circle": None, "depth": None, "counterbore_diameter": None, "counterbore_depth": None}] * 2}
    with pytest.raises(PlanInvalid, match="duplicate"):
        ClaudeJSON(client=FakeClient(json.dumps(bad))).call("s", [], BuildPlan)


def test_schemas_are_strict():
    for model in (DimensionSpec, BuildPlan):
        s = json.dumps(output_schema(model))
        assert '"const"' not in s and '"oneOf"' not in s and '"default"' not in s
        assert '"additionalProperties": false' in s


def test_planner_retry_includes_measured_failures():
    fake = FakeClient(from_plan(BuildPlan.model_validate(PLAN)).model_dump_json())
    prev = AttemptRecord(attempt=1, source="planner", plan=BuildPlan.model_validate(PLAN), report=VerificationReport(
        passed=False, model_tolerance_mm=0.01, validity=[], features=[], coverage=[],
        dimensions=[CheckResult(dim_id="D3", label="Ø8", kind="hole_diameter", expected="8.000", measured="7.800",
                                measured_value=7.8, deviation=-0.2, passed=False, method="Matched hole")]))
    ClaudePlanner(ClaudeJSON(client=fake), include_inputs=False).plan(DimensionSpec(), [], prev)
    text = fake.calls[0]["messages"][0]["content"][-1]["text"]
    assert "Verification failures to fix" in text and "D3 hole_diameter (Ø8): expected 8.000, measured 7.800" in text
    assert '"size_x":10.0' in text  # previous plan shown in the format the planner writes


def _count(node, pred):
    if isinstance(node, dict):
        return pred(node) + sum(_count(v, pred) for v in node.values())
    if isinstance(node, list):
        return sum(_count(v, pred) for v in node)
    return 0


@pytest.mark.parametrize("model", [DimensionSpec, WirePlan])
def test_schemas_fit_structured_output_limits(model):
    """Documented limits: at most 16 union-typed and 24 optional parameters."""
    s = output_schema(model)
    unions = _count(s, lambda n: int("anyOf" in n))
    optional = _count(s, lambda n: len(set(n.get("properties", {})) - set(n.get("required", []))) if n.get("type") == "object" else 0)
    assert unions <= 16 and optional == 0, (unions, optional)


def test_wire_format_round_trips_every_reference_plan():
    for c in CASES:
        assert to_plan(from_plan(c.build_plan())) == c.build_plan(), c.name


@pytest.mark.parametrize("patch, message", [
    (lambda w: w["operations"][0].update(direction="none"), "needs a direction"),
    (lambda w: w["operations"][0].update(positions=[[1, 2]]), r"must be \[x, y, z\]"),
    (lambda w: w["operations"][0].update(bolt_circle=[1, 2, 3]), "bolt_circle must be"),
])
def test_wire_errors_are_readable(patch, message):
    w = json.loads(from_plan(CASES[0].build_plan()).model_dump_json())
    patch(w)
    with pytest.raises(PlanInvalid, match=message):
        to_plan(WirePlan.model_validate(w))


def test_input_blocks_downscale_large_images_and_pass_pdfs(tmp_path: Path):
    big = tmp_path / "photo.jpg"
    Image.new("RGB", (4000, 3000), (200, 200, 200)).save(big, "JPEG")
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    blocks = input_blocks([big, pdf, tmp_path / "notes.txt"])
    assert [b["type"] for b in blocks] == ["image", "document"]
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"])))
    assert max(img.size) <= 2000


def test_extractor_needs_readable_inputs():
    with pytest.raises(LLMError, match="No readable inputs"):
        ClaudeExtractor(ClaudeJSON(client=FakeClient("{}"))).extract([], None)
