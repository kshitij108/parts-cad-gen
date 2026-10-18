"""The benchmark harness itself: oracle scores perfectly, mutants are all caught."""

import io
import json

from PIL import Image
from pypdf import PdfReader

from app.build.bench.cases import BY_NAME
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


def test_oracle_runs_every_input_variant(tmp_path):
    out, results = run_bench("oracle", ["plate_4_holes"], ["pdf", "scan", "photo"], tmp_path, 1)
    assert [r.variant for r in results] == ["pdf", "scan", "photo"]
    assert all(r.passed for r in results), [(r.variant, r.notes) for r in results]
    for r in results:
        assert (out / r.files["input_file"]).exists() and r.files["input"].endswith("input_preview.png")
