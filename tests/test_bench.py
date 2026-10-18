"""The benchmark harness itself: oracle scores perfectly, mutants are all caught."""

import json

from app.build.bench.run import extraction_recall, run_bench
from app.build.schemas import DimensionSpec


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
