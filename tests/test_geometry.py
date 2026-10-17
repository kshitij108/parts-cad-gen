import math

import pytest

from app.pipeline import geometry


@pytest.fixture(scope="module")
def bracket(step_dir):
    return geometry.run(step_dir / "bracket.step")


def test_bracket_volume_and_bbox(bracket):
    expected = 100 * 60 * 20 - math.pi * 5**2 * 20 - 40 * 20 * 10
    assert bracket.volume_mm3 == pytest.approx(expected, rel=1e-6)
    assert (bracket.bbox_mm.x, bracket.bbox_mm.y, bracket.bbox_mm.z) == pytest.approx((100, 60, 20), abs=1e-3)
    assert bracket.solid_count == 1


def test_bracket_hole_candidate(bracket):
    assert len(bracket.hole_candidates) == 1
    h = bracket.hole_candidates[0]
    assert h.diameter_mm == pytest.approx(10)
    assert h.depth_mm == pytest.approx(20)
    assert abs(h.axis.z) == pytest.approx(1)


def test_bracket_sharp_pocket_corners(bracket):
    vertical = [e for e in bracket.sharp_internal_edges if abs(e.direction.z) > 0.999]
    assert len(vertical) == 4  # four pocket wall corners
    assert len(bracket.sharp_internal_edges) == 8  # plus four floor edges


def test_thin_wall_detected(step_dir):
    s = geometry.run(step_dir / "thin.step")
    assert s.wall_thickness.min_mm == pytest.approx(0.6, abs=0.01)


def test_rejects_garbage(tmp_path):
    p = tmp_path / "bad.step"
    p.write_text("not a step file")
    with pytest.raises(geometry.GeometryError):
        geometry.run(p)
