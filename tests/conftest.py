from pathlib import Path

import pytest

from tests.builders import write_store


@pytest.fixture
def local_store(tmp_path: Path) -> Path:
    return write_store(tmp_path / "repository")
