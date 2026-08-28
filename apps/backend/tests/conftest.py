from __future__ import annotations

from pathlib import Path

import pytest

from tests.analytics_fixtures import create_analytical_dataset


@pytest.fixture
def analytical_dataset_path(tmp_path: Path) -> Path:
    return create_analytical_dataset(tmp_path)
