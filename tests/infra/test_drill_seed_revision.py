"""Tests for the revision-aware drill seeder.

The seeder exists to make one thing impossible: seeding an old database with the
*current* runtime. ``AER(...)`` migrates on construction, so a fixture built that
way would upgrade the store before the drill could migrate it -- and the drill
would then be measuring its own accident.

Most of these tests are therefore about what the seeder refuses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command

from aer import AER
from aer.storage.migrations import alembic_config, current_revision, head_revision


def build_revision(data_dir: Path, revision: str) -> Path:
    """A store at ``revision``, built by Alembic, with no rows."""
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "aer.db"
    command.upgrade(alembic_config(database), revision)
    return database


def tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def run(seeder: ModuleType, *args: str) -> int:
    return int(seeder.main(list(args)))


class TestSeedingARevision:
    def test_it_writes_every_table_that_revision_has(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        data_dir = tmp_path / "source"
        build_revision(data_dir, "0003")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 0

        with sqlite3.connect(data_dir / "aer.db") as connection:
            counts = {
                table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("runs", "events", "errors", "recoveries", "verifications")
            }
        assert counts == {
            "runs": 2,
            "events": 11,
            "errors": 1,
            "recoveries": 1,
            "verifications": 1,
        }

    def test_it_does_not_migrate_the_store(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        """The whole point: seeding must not be the thing that upgrades it."""
        data_dir = tmp_path / "source"
        database = build_revision(data_dir, "0003")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 0

        assert current_revision(database) == "0003"
        assert "experiences" not in tables(database)

    def test_a_revision_without_the_newer_table_does_not_gain_it(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        """At 0002 there is no verifications table, so no verdict may be written."""
        data_dir = tmp_path / "source-0002"
        database = build_revision(data_dir, "0002")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0002") == 0

        assert current_revision(database) == "0002"
        assert "verifications" not in tables(database)
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone()[0] == 1

    def test_the_links_between_records_are_real(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        """A recovery that points at nothing would not exercise the schema."""
        data_dir = tmp_path / "source"
        database = build_revision(data_dir, "0003")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 0

        with sqlite3.connect(database) as connection:
            error_event = connection.execute(
                "SELECT event_id FROM errors WHERE id = 'err-0003-1'"
            ).fetchone()[0]
            recovery = connection.execute(
                "SELECT error_id, start_event_id, result_event_id FROM recoveries "
                "WHERE id = 'rec-0003-1'"
            ).fetchone()
            verdict_event = connection.execute(
                "SELECT event_id FROM verifications WHERE id = 'ver-0003-1'"
            ).fetchone()[0]
            event_types = dict(connection.execute("SELECT id, event_type FROM events").fetchall())

        assert event_types[error_event] == "ERROR"
        assert recovery == ("err-0003-1", 9, 10)
        assert event_types[9] == "RECOVERY_START"
        assert event_types[10] == "RECOVERY_RESULT"
        assert event_types[verdict_event] == "VERIFICATION"


class TestRefusals:
    def test_it_refuses_a_store_at_another_revision(
        self, tmp_path: Path, drill_seed_revision: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Writing 0003 rows into a 0004 store would produce something that is neither."""
        data_dir = tmp_path / "wrong"
        build_revision(data_dir, "0004")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 1

        assert "not '0003'" in capsys.readouterr().err
        with sqlite3.connect(data_dir / "aer.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_it_refuses_an_unknown_revision(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        data_dir = tmp_path / "source"
        build_revision(data_dir, "0003")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0099") == 1

    def test_it_refuses_to_seed_twice(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        """Double-seeding would look like a drill failure that never happened."""
        data_dir = tmp_path / "source"
        build_revision(data_dir, "0003")

        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 0
        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 1

        with sqlite3.connect(data_dir / "aer.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2

    def test_it_refuses_a_missing_database(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        assert run(drill_seed_revision, str(tmp_path / "absent"), "--revision", "0003") == 1


class TestWhatTheMigrationThenSees:
    """The seeder's output has to survive the migration the drill is about."""

    def test_the_old_rows_are_readable_through_the_current_runtime(
        self, tmp_path: Path, drill_seed_revision: ModuleType
    ) -> None:
        data_dir = tmp_path / "source"
        database = build_revision(data_dir, "0003")
        assert run(drill_seed_revision, str(data_dir), "--revision", "0003") == 0

        command.upgrade(alembic_config(database), "head")

        assert current_revision(database) == head_revision()
        with AER(data_dir) as runtime:
            assert runtime.runs.count() == 2
            assert runtime.verified_success("run-0003-verified") is True
            assert runtime.errors.get("err-0003-1").resolved is True
            assert runtime.recoveries.get("rec-0003-1").success is True
            assert runtime.verifications.get("ver-0003-1").passed is True
            assert len(runtime.get_events("run-0003-recovered")) == 6
            # The tables 0004 added are present and empty: the migration created
            # schema, not business data.
            assert runtime.experiences.count() == 0
