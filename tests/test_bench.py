"""The benchmark harness itself: oracle scores perfectly, mutants are all caught."""

import copy
import io
import json

import pytest
from PIL import Image
from pypdf import PdfReader

import app.build.bench.run as run
from app.build.agent import StaticExtractor, StaticPlanner
from app.build.bench.cases import BY_NAME
from app.build.schemas import BuildPlan
from app.build.bench.inputs import EXIF_ORIENTATION, preview_png, render_input, upright
from app.build.bench.run import extraction_recall, run_bench
from app.build.builder import build
from app.build.derive import derive_spec
from app.build.schemas import DimensionSpec
from app.build.sheet import make_sheet


def test_oracle_and_mutant_runs(tmp_path):
    cases = ["block_pocket", "l_bracket", "photo_no_scale"]
    out, results = run_bench("oracle", cases, ["drawing", "sketch"], tmp_path, 1)
    assert all(r.passed for r in results), [(r.case, r.notes) for r in results]
    assert {r.variant for r in results} == {"drawing", "sketch", "picture"}
    assert all(r.iou == 1.0 for r in results if r.expect == "model")
    assert (out / "report.html").exists() and (out / "cases" / "block_pocket-sketch" / "input.png").exists()

    out, results = run_bench("mutant", cases, ["drawing"], tmp_path, 1)
    summary = json.loads((out / "report.json").read_text())["summary"]
    assert summary["mutations"] == 2 and summary["mutations_caught"] == 2
    assert summary["confident_wrong"] == 0


def test_extraction_recall_counts_matching_dimensions():
    gt = DimensionSpec.model_validate({"dimensions": [
        {"id": "D1", "label": "100", "kind": "overall", "nominal": 100, "axis": "x"},
        {"id": "D2", "label": "60", "kind": "overall", "nominal": 60, "axis": "y"}]})
    got = DimensionSpec.model_validate({"dimensions": [
        {"id": "A", "label": "100", "kind": "overall", "nominal": 100.004, "axis": "x"},
        {"id": "B", "label": "61", "kind": "overall", "nominal": 61, "axis": "y"}]})
    assert extraction_recall(gt, got) == 0.5


def test_input_variants_look_like_real_inputs():
    plan = BY_NAME["block_pocket"].build_plan()
    shape = build(plan)
    spec, _ = derive_spec(plan, shape)
    sheet = make_sheet(shape, spec, None, mode="input")

    name, pdf = render_input(sheet, "pdf", 1)
    assert name == "input.pdf" and "HOLE TABLE" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()

    name, tif = render_input(sheet, "scan", 1)
    scan = Image.open(io.BytesIO(tif))
    assert name == "input.tiff" and scan.format == "TIFF" and scan.mode == "L"
    assert scan.size[0] > 4961 and scan.size[1] > 3508  # A3 at 300 dpi, grown by the skew
    assert round(scan.info["dpi"][0]) == 300

    name, jpg = render_input(sheet, "photo", 1)
    photo = Image.open(io.BytesIO(jpg))
    assert name == "input.jpg" and photo.format == "JPEG"
    assert photo.getexif()[EXIF_ORIENTATION] == 6 and photo.size[1] > photo.size[0]  # stored sideways
    shown = upright(jpg)
    assert shown.size[0] > shown.size[1]  # a viewer that honours EXIF shows it landscape

    for variant, data in (("pdf", pdf), ("scan", tif), ("photo", jpg)):
        prev = Image.open(io.BytesIO(preview_png(sheet, variant, data)))
        assert prev.format == "PNG" and max(prev.size) <= 1600


# Audit B1 (parts-cad-ff): each wrong part still satisfies every ground-truth dimension, so only the
# shape comparison and the reference's feature checks can tell it apart from the real part.
WRONG_SHAPES = {
    "flange_bolt_circle": lambda p: p.__setitem__("stock", {"kind": "box", "size": {"x": 100, "y": 100, "z": 12}}),
    "boss_plate": lambda p: p["operations"][0]["base_center"].__setitem__("x", 40.6),
    "stepped_block": lambda p: p["operations"].append({"op": "pocket", "id": "PX", "center": {"x": 15, "y": 38, "z": 30},
                                                       "direction": "-z", "length": 20, "width": 16, "depth": 5}),
    "l_bracket": lambda p: p["stock"].__setitem__("points", [{"u": 0, "v": 0}, {"u": 80, "v": 0}, {"u": 80, "v": 6}, {"u": 6, "v": 6},
                                                             {"u": 6, "v": 45}, {"u": 3, "v": 50}, {"u": 0, "v": 50}]),
}


def _plan_with(planner_for):
    return lambda agent, case, gt_spec: (StaticExtractor(gt_spec), planner_for(case))


@pytest.mark.parametrize("name", list(WRONG_SHAPES))
def test_wrong_shape_never_scores_pass(name, tmp_path, monkeypatch):
    plan = copy.deepcopy(BY_NAME[name].plan)
    WRONG_SHAPES[name](plan)
    wrong = BuildPlan.model_validate(plan)
    monkeypatch.setattr(run, "agents_for", _plan_with(lambda c: StaticPlanner(wrong, source="planner")))
    res = run.run_case(BY_NAME[name], "drawing", "live-plan", tmp_path, 1)
    assert not res.passed
    assert res.iou is not None and res.iou < run.IOU_PASS
    assert res.confident_wrong == res.self_verified  # passing its own check while wrong is the headline failure


def test_invented_sizes_are_not_a_correct_refusal(tmp_path, monkeypatch):
    """Audit B2: a readiness issue alone isn't a refusal if the agent also made up sizes."""
    invented = DimensionSpec.model_validate({"scale_basis": "dimensioned", "dimensions": [
        {"id": "D1", "label": "120", "kind": "overall", "nominal": 120, "axis": "x"},
        {"id": "D2", "label": "80", "kind": "overall", "nominal": 80, "axis": "y"}]})  # Z missing, so the gate blocks
    monkeypatch.setattr(run, "agents_for", lambda agent, c, gt: (StaticExtractor(invented), StaticPlanner(c.build_plan())))
    res = run.run_case(BY_NAME["photo_no_scale"], "picture", "live", tmp_path, 1)
    assert not res.passed and any("Invented 2 size" in n for n in res.notes)

    counted = DimensionSpec.model_validate({"scale_basis": "none", "dimensions": [
        {"id": "D1", "label": "4X holes", "kind": "hole_count", "nominal": 4, "feature_diameter": 1}]})  # observable, not a size
    monkeypatch.setattr(run, "agents_for", lambda agent, c, gt: (StaticExtractor(counted), StaticPlanner(c.build_plan())))
    assert run.run_case(BY_NAME["photo_no_scale"], "picture", "live", tmp_path, 1).passed


def test_mutant_counts_only_verification_catches_and_keeps_crashes(tmp_path, monkeypatch):
    """Audit B3: a mutant that fails to build wasn't caught by verification, and a crashed case still counts."""
    unbuildable = lambda c: StaticPlanner(c.build_plan(), mutate=lambda d: (d["operations"][0].update(diameter=0), "diameter 0")[1],
                                          source="mutant")
    monkeypatch.setattr(run, "agents_for", _plan_with(unbuildable))
    real_case = run.run_case

    def crash_l_bracket(case, *a, **k):
        if case.name == "l_bracket":
            raise RuntimeError("boom")
        return real_case(case, *a, **k)

    monkeypatch.setattr(run, "run_case", crash_l_bracket)
    out, _ = run.run_bench("mutant", ["plate_4_holes", "l_bracket"], ["drawing"], tmp_path, 1)
    s = json.loads((out / "report.json").read_text())["summary"]
    assert s["mutations"] == 2 and s["mutations_caught"] == 0
    assert s["mutant_build_failures"] == 1 and s["errors"] == 1


def test_oracle_runs_every_input_variant(tmp_path):
    out, results = run_bench("oracle", ["plate_4_holes"], ["pdf", "scan", "photo"], tmp_path, 1)
    assert [r.variant for r in results] == ["pdf", "scan", "photo"]
    assert all(r.passed for r in results), [(r.variant, r.notes) for r in results]
    for r in results:
        assert (out / r.files["input_file"]).exists() and r.files["input"].endswith("input_preview.png")
