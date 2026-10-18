"""Drawings: generated from the model, readable as PDF/SVG/PNG, numbers from measurement."""

import io
import xml.etree.ElementTree as ET

import pytest
from PIL import Image
from pypdf import PdfReader

from app.build.bench.cases import BY_NAME
from app.build.builder import build
from app.build.derive import derive_spec
from app.build.sheet import make_sheet, project, to_pdf, to_png, to_svg
from app.build.verify import verify


@pytest.fixture(scope="module")
def part():
    plan = BY_NAME["block_pocket"].build_plan()
    shape = build(plan)
    spec, _ = derive_spec(plan, shape, part_name="Pocket block")
    return shape, spec, verify(shape, spec, plan)


def test_projection_matches_model_extents(part):
    shape, _, _ = part
    pts = [p for polys in project(shape, "top").values() for poly in polys for p in poly]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((0, 100, 0, 60), abs=1e-6)


def test_deliverable_pdf_has_measured_values_and_verification(part):
    shape, spec, report = part
    text = PdfReader(io.BytesIO(to_pdf(make_sheet(shape, spec, report)))).pages[0].extract_text()
    for s in ("Pocket block", "HOLE TABLE", "VERIFICATION", "100", "60", "25", "R5", "VERIFIED against 34"):
        assert s in text, s


def test_input_drawing_has_no_verification_or_ids(part):
    shape, spec, _ = part
    sheet = make_sheet(shape, spec, None, mode="input")
    texts = [l.text for l in sheet.labels]
    assert not any(t.startswith("VERIFICATION") for t in texts)
    assert not any(t.startswith("D") and t[1:2].isdigit() for t in texts)
    assert "50" in texts and "30" in texts and "12" in texts  # pocket size and depth on the views


def test_svg_and_png_render(part):
    shape, spec, report = part
    sheet = make_sheet(shape, spec, report)
    ET.fromstring(to_svg(sheet))  # well-formed
    for style in ("clean", "sketch"):
        img = Image.open(io.BytesIO(to_png(sheet, dpi=60, style=style)))
        assert img.size[0] > 900


def test_bare_picture_has_no_numbers(part):
    shape, _, _ = part
    sheet = make_sheet(shape, None, None, mode="input", views=("iso",), annotate=False, chrome=False)
    assert sheet.labels == [] and sheet.strokes
