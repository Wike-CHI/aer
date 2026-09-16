"""SQLite bootstrap tests (Milestone 2, Tasks 2.1 / 2.2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aer import AER, AERError, EventType
from aer.storage import head_revision, upgrade_to_head
from aer.storage.database import Database
from aer.storage.models import (
    ErrorRow,
    EventRow,
    ExperienceRow,
    ExperienceSourceRow,
    RecoveryRow,
    RunRow,
    VerificationRow,
)


class TestInitialisation:
    def test_data_directory_and_database_file_are_created(self, data_dir: Path) -> None:
        assert data_dir.exists() is False

        runtime = AER(data_dir)
        try:
            assert data_dir.is_dir()
            db_path = data_dir / "aer.db"
            assert db_path.is_file()
            assert runtime.database.path == db_path
            assert runtime.data_dir == data_dir
        finally:
            runtime.close()

    def test_the_database_is_usable_by_the_standard_sqlite_driver(self, aer: AER) -> None:
        """The file must be a plain SQLite database, not an AER-private format."""
        aer.start_run(task="task")

        with sqlite3.connect(aer.database.path) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM runs").fetchone()

        assert rows is not None
        assert rows[0] == 1

    def test_schema_contains_every_migrated_table(self, aer: AER) -> None:
        assert aer.database.table_names() == [
            "alembic_version",
            "errors",
            "events",
            "experience_sources",
            "experiences",
            "recoveries",
            "runs",
            "verifications",
        ]
        assert RunRow.__tablename__ == "runs"
        assert EventRow.__tablename__ == "events"
        assert ErrorRow.__tablename__ == "errors"
        assert RecoveryRow.__tablename__ == "recoveries"
        assert VerificationRow.__tablename__ == "verifications"
        assert ExperienceRow.__tablename__ == "experiences"
        assert ExperienceSourceRow.__tablename__ == "experience_sources"

    def test_migrations_are_recorded_and_reapplying_them_is_safe(self, aer: AER) -> None:
        assert aer.database.schema_revision() == head_revision()

        upgrade_to_head(aer.database.path)
        upgrade_to_head(aer.database.path)

        assert aer.database.schema_revision() == head_revision()
        assert aer.database.table_names() == [
            "alembic_version",
            "errors",
            "events",
            "experience_sources",
            "experiences",
            "recoveries",
            "runs",
            "verifications",
        ]

    def test_reopening_an_existing_directory_keeps_the_data(self, data_dir: Path) -> None:
        first = AER(data_dir)
        context = first.start_run(task="task")
        first.close()

        second = AER(data_dir)
        try:
            assert second.get_run(context.run_id) is not None
        finally:
            second.close()


class TestPragmas:
    def test_wal_journal_mode_is_enabled(self, aer: AER) -> None:
        assert aer.database.read_pragmas()["journal_mode"] == "wal"

    def test_foreign_keys_are_enforced(self, aer: AER) -> None:
        assert aer.database.read_pragmas()["foreign_keys"] == 1

    def test_synchronous_is_normal(self, aer: AER) -> None:
        # SQLite reports NORMAL as the integer 1.
        assert aer.database.read_pragmas()["synchronous"] == 1

    def test_busy_timeout_is_configured(self, aer: AER) -> None:
        assert aer.database.read_pragmas()["busy_timeout"] == 5000

    def test_wal_sidecar_file_appears_once_data_is_written(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.emit(EventType.MODEL_CALL)

        wal_path = Path(f"{aer.database.path}-wal")
        assert wal_path.is_file()

    def test_pragmas_are_applied_to_every_new_connection(self, data_dir: Path) -> None:
        """``foreign_keys`` is per-connection, so it must survive pool recycling."""
        database = Database(data_dir / "aer.db")
        try:
            first = database.read_pragmas()

            # Force the pool to hand out different connections.
            for _ in range(5):
                with database.session():
                    pass

            second = database.read_pragmas()
            assert first == second
            assert second["foreign_keys"] == 1
            assert second["journal_mode"] == "wal"
        finally:
            database.dispose()


class TestLifecycle:
    def test_close_is_idempotent(self, aer: AER) -> None:
        aer.close()
        aer.close()

        assert aer.is_closed is True

    def test_operations_after_close_fail_loudly(self, aer: AER) -> None:
        aer.close()

        with pytest.raises(AERError, match="already closed"):
            aer.start_run(task="task")

        with pytest.raises(AERError, match="already closed"):
            aer.get_run("any")

        with pytest.raises(AERError, match="already closed"):
            aer.list_runs()

        with pytest.raises(AERError, match="already closed"):
            aer.get_events("any")

    def test_supports_the_context_manager_protocol(self, data_dir: Path) -> None:
        with AER(data_dir) as runtime:
            context = runtime.start_run(task="task")
            assert runtime.get_run(context.run_id) is not None

        assert runtime.is_closed is True
