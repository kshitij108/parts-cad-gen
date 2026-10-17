import json
from pathlib import Path

import pytest

from app.schemas import RateCard
from tests.fixtures import bracket_shape, thin_wall_shape, write_step

EXAMPLE = Path(__file__).parent.parent / "app" / "example_rate_card.json"


@pytest.fixture(scope="session")
def step_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("steps")
    write_step(bracket_shape(), d / "bracket.step")
    write_step(thin_wall_shape(), d / "thin.step")
    return d


@pytest.fixture
def rate_card() -> RateCard:
    return RateCard.model_validate(json.loads(EXAMPLE.read_text()))
