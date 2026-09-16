"""Where does `alembic upgrade head` actually write?

This is a contract, not an implementation detail: if the CLI resolves a different
file from the runtime, a deployment migrates one database and serves another --
and the mistake is invisible, because both files exist and both look fine.

Written against the real CLI in a subprocess rather than by calling
``resolve_db_path()`` directly: the failure mode this guards is "the resolver is
right but the entry point ignores it", which only a real invocation can catch.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def alembic_executable() -> str:
    """The `alembic` entry point that belongs to the interpreter running the tests.

    Checked next to ``sys.executable`` first: in a virtualenv that is the same
    installation the package came from, whereas a bare ``alembic`` on PATH could be
    a different environment entirely.
    """
    sibling = Path(sys.executable).parent / "alembic"
    for candidate in (sibling, sibling.with_suffix(".exe")):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("alembic")
    if found is None:  # pragma: no cover - alembic is a runtime dependency
        raise AssertionError("the alembic CLI is not installed; run `pip install -e '.[dev]'`")
    return found


def run_alembic(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI from the repository root with a controlled environment."""
    # The repository itself must not leak into the resolution: every case below
    # sets the variable it is about, and the ambient ones would silently win.
    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in {"AER_DATA_DIR", "AER_DB_PATH"}
    }
    clean.update(env)
    return subprocess.run(
        [alembic_executable(), *args],
        cwd=REPOSITORY_ROOT,
        env=clean,
        capture_output=True,
        text=True,
        check=False,
    )


def revision_of(db_path: Path) -> str | None:
    """The Alembic revision recorded in a database file."""
    if not db_path.is_file():
        return None
    with closing(sqlite3.connect(str(db_path))) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else None


class TestResolution:
    def test_a_data_directory_alone_is_enough(self, tmp_path: Path) -> None:
        """The container case: AER_DATA_DIR is the only variable that is set.

        The directory deliberately does not exist yet, because that is what a first
        deployment onto a fresh host looks like -- and the CLI has to create the
        store rather than fail with a raw driver error.
        """
        data_dir = tmp_path / "aer" / "data"
        assert not data_dir.exists()

        completed = run_alembic("upgrade", "head", env={"AER_DATA_DIR": str(data_dir)})

        assert completed.returncode == 0, completed.stderr
        assert revision_of(data_dir / "aer.db") is not None

    def test_the_explicit_file_name_wins_over_the_directory(self, tmp_path: Path) -> None:
        """AER_DB_PATH is the more specific instruction, so it takes precedence."""
        data_dir = tmp_path / "unused-data-dir"
        explicit = tmp_path / "explicit.db"

        completed = run_alembic(
            "upgrade", "head", env={"AER_DATA_DIR": str(data_dir), "AER_DB_PATH": str(explicit)}
        )

        assert completed.returncode == 0, completed.stderr
        assert revision_of(explicit) is not None
        assert not data_dir.exists(), "the superseded directory must not be touched"

    def test_the_dash_x_argument_wins_over_both_variables(self, tmp_path: Path) -> None:
        """`-x db_path=...` is how deploy.sh makes its intent unambiguous."""
        data_dir = tmp_path / "ignored-dir"
        explicit = tmp_path / "from-x.db"

        completed = run_alembic(
            "-x",
            f"db_path={explicit}",
            "upgrade",
            "head",
            env={
                "AER_DATA_DIR": str(data_dir),
                "AER_DB_PATH": str(tmp_path / "ignored-file.db"),
            },
        )

        assert completed.returncode == 0, completed.stderr
        assert revision_of(explicit) is not None

    def test_the_runtime_and_the_cli_agree(self, tmp_path: Path) -> None:
        """The invariant that makes all of the above worth testing.

        The embedded runtime is asked for its database path under the same
        environment the CLI ran with, and the two must name the same file.
        """
        from aer import AER
        from aer.config import load_deployment_config

        data_dir = tmp_path / "srv" / "aer" / "data"
        # The full production environment, not just the database location:
        # AER_ENV=production requires every path to be absolute, so a partial
        # environment is rejected -- which is itself the subject of a test in
        # test_deployment_config.py.
        environment = {
            "AER_ENV": "production",
            "AER_DATA_DIR": str(data_dir),
            "AER_ARTIFACT_DIR": str(tmp_path / "srv" / "aer" / "artifacts"),
            "AER_KNOWLEDGE_DIR": str(tmp_path / "srv" / "aer" / "knowledge"),
            "AER_BACKUP_DIR": str(tmp_path / "srv" / "aer" / "backups"),
        }

        completed = run_alembic("upgrade", "head", env=environment)
        assert completed.returncode == 0, completed.stderr

        config = load_deployment_config(environment)
        with AER(config.db_path.parent, db_filename=config.db_path.name) as runtime:
            assert runtime.database.path == config.db_path
            assert revision_of(config.db_path) is not None
