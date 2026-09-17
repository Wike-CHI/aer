"""Fixtures for the infrastructure tests.

The ops scripts are *files*, not an importable package (see README.md): that is a
deliberate property of the design, because they must be runnable as
``python scripts/backup_sqlite.py`` both in a checkout and inside the container.
Tests therefore load them by path.

Loading by path rather than by adding ``scripts/`` to the import path keeps the
test suite honest about one thing: if a script's sibling import only works because
a developer's PYTHONPATH happens to help, the test fails here rather than in a
deployment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: Repository root: tests/infra/conftest.py -> tests/infra -> tests -> root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
DEPLOY_DIR = REPOSITORY_ROOT / "deploy"
WORKFLOWS_DIR = REPOSITORY_ROOT / ".github" / "workflows"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` as a module, once per session."""
    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    path = SCRIPTS_DIR / f"{name}.py"
    if not path.is_file():
        raise AssertionError(f"missing ops script: {path.as_posix()}")

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise AssertionError(f"cannot load {path.as_posix()}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so a script that imports a sibling resolves to
    # the same module object the tests hold.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def backup_sqlite() -> ModuleType:
    """The ``backup_sqlite`` ops module."""
    return load_script("backup_sqlite")


@pytest.fixture(scope="session")
def restore_sqlite() -> ModuleType:
    """The ``restore_sqlite`` ops module."""
    return load_script("restore_sqlite")


@pytest.fixture(scope="session")
def smoke_test() -> ModuleType:
    """The ``smoke_test`` ops module."""
    return load_script("smoke_test")


@pytest.fixture(scope="session")
def drill_facts() -> ModuleType:
    """The ``drill_facts`` module: read-only database inspection for drills."""
    return load_script("drill_facts")


@pytest.fixture(scope="session")
def drill_compare() -> ModuleType:
    """The ``drill_compare`` module: byte-level and logical file comparison."""
    return load_script("drill_compare")


@pytest.fixture(scope="session")
def drill_seed() -> ModuleType:
    """The ``drill_seed`` module: a sandbox store holding one of every record."""
    return load_script("drill_seed")


@pytest.fixture
def ops_root(tmp_path: Path) -> Path:
    """An isolated stand-in for ``/srv/aer``: data, artifacts, knowledge, backups."""
    root = tmp_path / "srv-aer"
    for name in ("data", "artifacts", "knowledge", "backups"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root
