"""Alembic migration tests (Milestones 3-4; round-3 brief sections 2-4 and 32,
round-4 brief section 35).

Four things must hold:

1. an empty database reaches ``head`` and gets exactly the expected tables;
2. a database created **before** Alembic was adopted is adopted in place -- no
   deletion, no data migration, no manual stamping;
3. a database already at the previous revision is upgraded in place, keeping every
   row it held;
4. the schema produced by the revision history is identical to the schema
   ``Base.metadata`` describes, so models and migrations cannot drift apart.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine

from aer.storage.connection import sqlite_url
from aer.storage.migrations import (
    alembic_config,
    current_revision,
    head_revision,
    is_at_head,
    upgrade_to_head,
)
from aer.storage.models import (
    Base,
    ErrorRow,
    EventRow,
    ExperienceRow,
    ExperienceSourceRow,
    RecoveryRow,
    RunRow,
    VerificationRow,
)

EXPECTED_TABLES = {
    "alembic_version",
    "errors",
    "events",
    "experience_sources",
    "experiences",
    "recoveries",
    "runs",
    "verifications",
}

#: The newest revision. Pinned so that adding one is a conscious edit rather than a
#: silently passing test.
HEAD_REVISION = "0004"


def read_schema(db_path: Path) -> dict[str, str]:
    """Map every named object in ``sqlite_master`` to its DDL."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {name: sql for name, sql in rows}


def table_names(db_path: Path) -> set[str]:
    """User tables only: ``sqlite_sequence`` is SQLite's own bookkeeping."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {name for (name,) in rows}


_CONSTRAINT_PREFIXES = ("FOREIGN KEY", "PRIMARY KEY", "UNIQUE", "CONSTRAINT", "CHECK")


def normalise_ddl(ddl: str) -> str:
    """Order-insensitive form of one DDL statement.

    Alembic and ``create_all`` emit the same constraints in a different order
    (Alembic sorts foreign keys by column, SQLAlchemy by declaration). That has no
    effect on the resulting database, so the parity check compares the set of
    definitions -- while keeping column order, which does matter.
    """
    lines = [line.strip().rstrip(",") for line in ddl.splitlines()]
    lines = [line for line in lines if line and line != ")"]
    if not lines:
        return ""
    columns = [line for line in lines[1:] if not line.upper().startswith(_CONSTRAINT_PREFIXES)]
    constraints = sorted(
        line for line in lines[1:] if line.upper().startswith(_CONSTRAINT_PREFIXES)
    )
    return "\n".join([lines[0], *columns, *constraints])


class TestUpgradeFromEmpty:
    def test_creates_the_file_and_reaches_head(self, tmp_path: Path) -> None:
        db = tmp_path / "empty.db"
        assert not db.exists()

        upgrade_to_head(db)

        assert db.is_file()
        assert current_revision(db) == head_revision()
        assert is_at_head(db) is True
        assert table_names(db) == EXPECTED_TABLES

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        db = tmp_path / "nested" / "deeper" / "aer.db"

        upgrade_to_head(db)

        assert db.is_file()
        assert is_at_head(db) is True

    def test_applying_twice_changes_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "twice.db"
        upgrade_to_head(db)
        before = read_schema(db)

        upgrade_to_head(db)

        assert read_schema(db) == before

    def test_records_the_revision_in_the_database(self, tmp_path: Path) -> None:
        db = tmp_path / "recorded.db"
        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            stored = connection.execute("SELECT version_num FROM alembic_version").fetchone()

        assert stored is not None
        assert stored[0] == head_revision()

    def test_an_unmigrated_file_reports_no_revision(self, tmp_path: Path) -> None:
        db = tmp_path / "untouched.db"

        assert current_revision(db) is None
        assert is_at_head(db) is False

    def test_works_from_any_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Alembic config is located relative to the package, not the CWD."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        db = tmp_path / "cwd.db"

        upgrade_to_head(db)

        assert is_at_head(db) is True


class TestAdoptionOfAPreAlembicDatabase:
    """Milestone 1-2 databases were built with ``create_all`` and hold real data.

    Bringing them under migration control must not require deleting ``aer.db``
    (round-3 brief, section 2).
    """

    @staticmethod
    def build_legacy_database(path: Path) -> None:
        """Create exactly the two tables Milestone 2 shipped, plus one run."""
        engine = create_engine(sqlite_url(path))
        Base.metadata.create_all(engine, tables=[RunRow.__table__, EventRow.__table__])
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO runs "
                "(id, task_type, task_description, status, started_at, metadata_json) "
                "VALUES ('legacy-run', 'wordpress', 'legacy task', 'SUCCESS', "
                "'2026-09-15 10:00:00.000000', '{\"legacy\": true}')"
            )
            connection.exec_driver_sql(
                "INSERT INTO events (run_id, sequence, event_type, created_at, metadata_json) "
                "VALUES ('legacy-run', 1, 'TASK_START', '2026-09-15 10:00:00.000000', '{}')"
            )
        engine.dispose()

    def test_upgrade_adopts_the_existing_schema_in_place(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        self.build_legacy_database(db)

        assert current_revision(db) is None
        assert table_names(db) == {"runs", "events"}

        upgrade_to_head(db)

        assert current_revision(db) == head_revision()
        assert table_names(db) == EXPECTED_TABLES

    def test_existing_rows_survive_the_upgrade(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy-data.db"
        self.build_legacy_database(db)

        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            run = connection.execute(
                "SELECT id, task_description, status, metadata_json FROM runs"
            ).fetchone()
            events = connection.execute("SELECT run_id, sequence FROM events").fetchall()

        assert run == ("legacy-run", "legacy task", "SUCCESS", '{"legacy": true}')
        assert events == [("legacy-run", 1)]

    def test_the_head_revision_is_where_we_think_it_is(self, tmp_path: Path) -> None:
        db = tmp_path / "head.db"
        upgrade_to_head(db)

        assert head_revision() == HEAD_REVISION
        assert current_revision(db) == HEAD_REVISION

    def test_the_new_tables_are_usable_after_adoption(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy-usable.db"
        self.build_legacy_database(db)
        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO errors "
                "(id, run_id, event_id, error_type, error_message, recoverable, resolved, "
                " created_at, metadata_json) "
                "VALUES ('e1', 'legacy-run', 1, 'builtins.RuntimeError', 'boom', 1, 0, "
                "'2026-09-16 10:00:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO recoveries "
                "(id, run_id, error_id, reason, success, started_at) "
                "VALUES ('r1', 'legacy-run', 'e1', 'fixed it', 1, "
                "'2026-09-16 10:00:01.000000')"
            )
            connection.execute(
                "INSERT INTO verifications "
                "(id, run_id, event_id, verifier_type, verifier_name, passed, required, "
                " created_at) "
                "VALUES ('v1', 'legacy-run', 1, 'DETERMINISTIC', 'http_status', 1, 1, "
                "'2026-09-16 10:00:02.000000')"
            )
            connection.commit()
            assert connection.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1


class TestUpgradeFromThePreviousRevision:
    """Revision 0003 must land on a database that is already at 0002.

    This is the ordinary production path from here on: a store created during
    Milestone 3 already holds runs, events, errors and recoveries, and it must gain
    the verification table without losing any of them.
    """

    @staticmethod
    def build_milestone_three_database(path: Path) -> None:
        """Migrate to ``0002`` and write one row of each Milestone 3 kind."""
        command.upgrade(alembic_config(path), "0002")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO runs "
                "(id, task_description, status, started_at, metadata_json) "
                "VALUES ('run-m3', 'milestone three task', 'SUCCESS', "
                "'2026-09-16 09:00:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO events (id, run_id, sequence, event_type, created_at) "
                "VALUES (1, 'run-m3', 1, 'TASK_START', '2026-09-16 09:00:00.000000')"
            )
            connection.execute(
                "INSERT INTO errors "
                "(id, run_id, event_id, error_type, error_message, recoverable, resolved, "
                " created_at) "
                "VALUES ('err-m3', 'run-m3', 1, 'builtins.RuntimeError', 'boom', 1, 1, "
                "'2026-09-16 09:00:01.000000')"
            )
            connection.execute(
                "INSERT INTO recoveries (id, run_id, error_id, reason, success, started_at) "
                "VALUES ('rec-m3', 'run-m3', 'err-m3', 'fixed', 1, "
                "'2026-09-16 09:00:02.000000')"
            )
            connection.commit()

    def test_the_database_starts_at_the_previous_revision(self, tmp_path: Path) -> None:
        db = tmp_path / "m3.db"
        self.build_milestone_three_database(db)

        assert current_revision(db) == "0002"
        assert table_names(db) == {"alembic_version", "errors", "events", "recoveries", "runs"}

    def test_upgrading_reaches_head_in_place(self, tmp_path: Path) -> None:
        db = tmp_path / "m3-upgrade.db"
        self.build_milestone_three_database(db)

        upgrade_to_head(db)

        assert current_revision(db) == head_revision() == HEAD_REVISION
        assert table_names(db) == EXPECTED_TABLES

    def test_milestone_three_rows_survive_the_upgrade(self, tmp_path: Path) -> None:
        db = tmp_path / "m3-data.db"
        self.build_milestone_three_database(db)

        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            run = connection.execute("SELECT id, status FROM runs WHERE id = 'run-m3'").fetchone()
            error = connection.execute(
                "SELECT error_type, resolved FROM errors WHERE id = 'err-m3'"
            ).fetchone()
            recovery = connection.execute(
                "SELECT reason, success FROM recoveries WHERE id = 'rec-m3'"
            ).fetchone()
            sequence = connection.execute(
                "SELECT sequence, event_type FROM events WHERE run_id = 'run-m3'"
            ).fetchone()

        assert run == ("run-m3", "SUCCESS")
        assert error == ("builtins.RuntimeError", 1)
        assert recovery == ("fixed", 1)
        assert sequence == (1, "TASK_START")

    def test_a_verdict_can_be_recorded_against_a_pre_existing_run(self, tmp_path: Path) -> None:
        db = tmp_path / "m3-verified.db"
        self.build_milestone_three_database(db)
        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO verifications "
                "(id, run_id, verifier_type, verifier_name, passed, required, message, "
                " created_at) "
                "VALUES ('v-post', 'run-m3', 'ENVIRONMENT', 'live_h1', 0, 1, "
                "'found 0 <h1>, expected 1', '2026-09-16 11:00:00.000000')"
            )
            connection.commit()
            stored = connection.execute(
                "SELECT passed, required, message FROM verifications WHERE id = 'v-post'"
            ).fetchone()

        assert stored == (0, 1, "found 0 <h1>, expected 1")


class TestSchemaParity:
    def test_migrated_schema_matches_the_orm_metadata(self, tmp_path: Path) -> None:
        """Models and revisions must describe the same database.

        Without this check, adding a column to ``aer/storage/models.py`` and
        forgetting a revision would only be noticed in production.
        """
        migrated = tmp_path / "migrated.db"
        upgrade_to_head(migrated)

        from_metadata = tmp_path / "metadata.db"
        engine = create_engine(sqlite_url(from_metadata))
        Base.metadata.create_all(engine)
        engine.dispose()

        migrated_schema = read_schema(migrated)
        metadata_schema = read_schema(from_metadata)
        assert metadata_schema, "create_all produced no schema to compare against"

        for name, ddl in sorted(metadata_schema.items()):
            assert name in migrated_schema, f"{name} is missing from the migrated schema"
            assert normalise_ddl(migrated_schema[name]) == normalise_ddl(ddl), (
                f"DDL differs for {name}"
            )

    def test_the_autoincrement_keyword_survives_generation(self, tmp_path: Path) -> None:
        """``events.id`` must stay ``INTEGER PRIMARY KEY AUTOINCREMENT``."""
        db = tmp_path / "autoincrement.db"
        upgrade_to_head(db)

        assert "AUTOINCREMENT" in read_schema(db)["events"]

    def test_foreign_keys_and_cascade_survive_generation(self, tmp_path: Path) -> None:
        db = tmp_path / "fks.db"
        upgrade_to_head(db)
        schema = read_schema(db)

        assert "ON DELETE CASCADE" in schema["events"]
        assert "ON DELETE CASCADE" in schema["errors"]
        assert "ON DELETE CASCADE" in schema["recoveries"]
        assert "ON DELETE CASCADE" in schema["verifications"]
        assert "ON DELETE CASCADE" in schema["experience_sources"]
        assert "ON DELETE SET NULL" in schema["errors"]
        assert "ON DELETE SET NULL" in schema["recoveries"]
        assert "ON DELETE SET NULL" in schema["verifications"]

    def test_the_experience_source_key_survives_generation(self, tmp_path: Path) -> None:
        """The composite key is what makes a duplicate link impossible."""
        db = tmp_path / "source-key.db"
        upgrade_to_head(db)

        assert "PRIMARY KEY (experience_id, run_id)" in read_schema(db)["experience_sources"]

    def test_the_dedup_key_is_indexed_but_not_unique(self, tmp_path: Path) -> None:
        """A normalised key must never be able to reject a legitimate experience."""
        db = tmp_path / "dedup.db"
        upgrade_to_head(db)
        schema = read_schema(db)

        assert "ix_experiences_dedup_key" in schema
        assert "UNIQUE" not in schema["experiences"]

    def test_the_sequence_unique_constraint_survives_generation(self, tmp_path: Path) -> None:
        db = tmp_path / "unique.db"
        upgrade_to_head(db)

        assert "uq_events_run_sequence" in read_schema(db)["events"]

    def test_every_declared_table_is_covered_by_the_history(self, tmp_path: Path) -> None:
        """Guards the other direction: a model with no revision behind it."""
        db = tmp_path / "coverage.db"
        upgrade_to_head(db)

        assert set(Base.metadata.tables) == {
            "runs",
            "events",
            "errors",
            "recoveries",
            "verifications",
            "experiences",
            "experience_sources",
        }
        assert table_names(db) == EXPECTED_TABLES
        assert ErrorRow.__tablename__ == "errors"
        assert RecoveryRow.__tablename__ == "recoveries"
        assert VerificationRow.__tablename__ == "verifications"
        assert ExperienceRow.__tablename__ == "experiences"
        assert ExperienceSourceRow.__tablename__ == "experience_sources"
