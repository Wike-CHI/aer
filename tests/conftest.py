"""Shared pytest fixtures.

Every test runs against a throwaway SQLite file inside pytest's ``tmp_path``:
no test touches ``./data`` and no test depends on another test's state.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aer import AER


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Path to a data directory that does not exist yet."""
    return tmp_path / "data"


@pytest.fixture
def aer(data_dir: Path) -> Iterator[AER]:
    """An open embedded runtime; always closed, even when a test fails."""
    runtime = AER(data_dir)
    try:
        yield runtime
    finally:
        runtime.close()
