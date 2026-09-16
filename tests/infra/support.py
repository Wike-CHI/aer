"""Shared helpers for the infrastructure tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from aer import AER

#: Repository root: tests/infra/support.py -> tests/infra -> tests -> root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def repo_file(relative: str) -> Path:
    """A path inside the repository."""
    return REPOSITORY_ROOT / relative


def read_repo_file(relative: str) -> str:
    """Read a repository file as UTF-8 text."""
    return repo_file(relative).read_text(encoding="utf-8")


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """A raw SQLite connection that is *closed* on exit.

    ``with sqlite3.connect(...)`` manages the transaction, not the connection, so
    tests that used it would leave file handles open until garbage collection --
    which on Windows means a ``tmp_path`` that cannot be cleaned up.
    """
    with closing(sqlite3.connect(str(db_path))) as connection:
        yield connection


def row_count(db_path: str | Path, table: str) -> int:
    """Number of rows in ``table``."""
    with connect(db_path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def seed_runs(data_dir: str | Path, tasks: Sequence[str]) -> list[str]:
    """Create a real AER store containing one finished run per task name."""
    identifiers: list[str] = []
    with AER(data_dir) as runtime:
        for task in tasks:
            run = runtime.start_run(task=task, task_type="seed")
            run.success()
            identifiers.append(run.run_id)
    return identifiers


def task_descriptions(data_dir: str | Path) -> list[str]:
    """Task descriptions recorded in a store, read directly so no migration runs."""
    with connect(Path(data_dir) / "aer.db") as connection:
        rows = connection.execute("SELECT task_description FROM runs ORDER BY task_description")
        return [str(row[0]) for row in rows]
