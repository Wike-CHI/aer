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
    AdapterEventRow,
    AdapterSessionRow,
    Base,
    ErrorRow,
    EventRow,
    ExperienceRow,
    ExperienceSourceRow,
    ExperienceUsageRow,
    RecoveryRow,
    RetrievalSessionRow,
    RunRow,
    VerificationRow,
)

EXPECTED_TABLES = {
    "adapter_events",
    "adapter_sessions",
    "alembic_version",
    "errors",
    "events",
    "experience_sources",
    "experience_usage",
    "experiences",
    "recoveries",
    "retrieval_sessions",
    "runs",
    "verifications",
}

#: The newest revision. Pinned so that adding one is a conscious edit rather than a
#: silently passing test.
HEAD_REVISION = "0006"


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


class TestUpgradeFromMilestoneSix:
    """Revision 0005 must land on a database that is already at 0004.

    The Milestone 7 production path. A live store already holds distilled
    experiences, and after the upgrade it must still hold exactly those, with two
    empty tables beside them. "Empty" is the expected state rather than a symptom: a
    real deployment has recorded no usage yet, because nothing has retrieved through
    the tracked API until the new code runs (round-7 brief, sections 76-77).
    """

    @staticmethod
    def build_milestone_seven_predecessor(path: Path) -> None:
        """Migrate to ``0004`` and write one run, one event, one experience, one link."""
        command.upgrade(alembic_config(path), "0004")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO runs "
                "(id, task_description, status, started_at, metadata_json) "
                "VALUES ('run-m6', 'milestone six task', 'SUCCESS', "
                "'2026-09-18 09:00:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO events (id, run_id, sequence, event_type, created_at) "
                "VALUES (1, 'run-m6', 1, 'TASK_START', '2026-09-18 09:00:00.000000')"
            )
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, status, confidence, generalizable, "
                " outcome_verified, dedup_key, created_at, updated_at, metadata_json) "
                "VALUES ('exp-m6', 'RECOVERY', 'wordpress', 'REST API 403', "
                "'403 on page update', 'VERIFIED', 0.0, 1, 1, 'RECOVERY|wordpress|403', "
                "'2026-09-18 09:01:00.000000', '2026-09-18 09:01:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO experience_sources (experience_id, run_id, created_at) "
                "VALUES ('exp-m6', 'run-m6', '2026-09-18 09:01:00.000000')"
            )
            connection.commit()

    def test_the_database_starts_at_the_previous_revision(self, tmp_path: Path) -> None:
        db = tmp_path / "m6.db"
        self.build_milestone_seven_predecessor(db)

        assert current_revision(db) == "0004"
        assert table_names(db) == {
            "alembic_version",
            "errors",
            "events",
            "experience_sources",
            "experiences",
            "recoveries",
            "runs",
            "verifications",
        }

    def test_upgrading_reaches_head_in_place(self, tmp_path: Path) -> None:
        db = tmp_path / "m6-upgrade.db"
        self.build_milestone_seven_predecessor(db)

        upgrade_to_head(db)

        assert current_revision(db) == head_revision() == HEAD_REVISION
        assert table_names(db) == EXPECTED_TABLES

    def test_experiences_survive_and_the_new_tables_arrive_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "m6-data.db"
        self.build_milestone_seven_predecessor(db)

        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            experience = connection.execute(
                "SELECT kind, status, outcome_verified, dedup_key "
                "FROM experiences WHERE id = 'exp-m6'"
            ).fetchone()
            links = connection.execute(
                "SELECT experience_id, run_id FROM experience_sources"
            ).fetchall()
            sessions = connection.execute("SELECT COUNT(*) FROM retrieval_sessions").fetchone()
            usage = connection.execute("SELECT COUNT(*) FROM experience_usage").fetchone()

        assert experience == ("RECOVERY", "VERIFIED", 1, "RECOVERY|wordpress|403")
        assert links == [("exp-m6", "run-m6")]
        assert sessions[0] == 0
        assert usage[0] == 0

    def test_usage_can_be_recorded_against_a_pre_existing_experience(self, tmp_path: Path) -> None:
        """The new tables must be usable against rows the migration did not create."""
        db = tmp_path / "m6-usable.db"
        self.build_milestone_seven_predecessor(db)
        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO retrieval_sessions "
                "(id, run_id, query_text, query_fingerprint, domain, mode, "
                " requested_limit, result_count, knowledge_projection_version, "
                " retrieval_policy_version, retrieval_duration_ms, assignment, created_at) "
                "VALUES ('sess-1', 'run-m6', 'rest api 403', 'fp', 'wordpress', 'GUIDANCE', "
                "3, 1, 1, '1', 12, 'NONE', '2026-09-20 09:00:00.000000')"
            )
            connection.execute(
                "INSERT INTO experience_usage "
                "(id, retrieval_session_id, experience_id, rank, role, retrieval_score, "
                " retrieved_at, usage_signal, utility_label, created_at, updated_at) "
                "VALUES ('use-1', 'sess-1', 'exp-m6', 1, 'GUIDANCE', 0.8, "
                "'2026-09-20 09:00:00.000000', 'UNKNOWN', 'UNKNOWN', "
                "'2026-09-20 09:00:00.000000', '2026-09-20 09:00:00.000000')"
            )
            connection.commit()
            stored = connection.execute(
                "SELECT experience_id, usage_signal FROM experience_usage WHERE id = 'use-1'"
            ).fetchone()

        assert stored == ("exp-m6", "UNKNOWN")


class TestUpgradeFromMilestoneSeven:
    """Revision 0006 must land on a database that is already at 0005.

    The Milestone 8 production path. A live store already holds verification verdicts
    and usage rows; after the upgrade it must still hold exactly those, with the two
    adapter tables empty beside them. Empty is correct rather than suspicious: a
    deployment that has not yet wired an Agent integration has no external sessions and
    has accepted no external events (round-8 brief, sections 52 and 59).
    """

    @staticmethod
    def build_milestone_eight_predecessor(path: Path) -> None:
        """Migrate to ``0005`` and write one run, one experience, one usage row."""
        command.upgrade(alembic_config(path), "0005")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO runs "
                "(id, task_description, status, started_at, metadata_json) "
                "VALUES ('run-m7', 'milestone seven task', 'SUCCESS', "
                "'2026-09-20 09:00:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO events (id, run_id, sequence, event_type, created_at) "
                "VALUES (1, 'run-m7', 1, 'TASK_START', '2026-09-20 09:00:00.000000')"
            )
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, status, confidence, generalizable, "
                " outcome_verified, dedup_key, created_at, updated_at, metadata_json) "
                "VALUES ('exp-m7', 'RECOVERY', 'wordpress', '403', '403 on update', "
                "'VERIFIED', 0.0, 1, 1, 'RECOVERY|wordpress|403', "
                "'2026-09-20 09:01:00.000000', '2026-09-20 09:01:00.000000', '{}')"
            )
            connection.execute(
                "INSERT INTO retrieval_sessions "
                "(id, run_id, query_text, query_fingerprint, domain, mode, "
                " requested_limit, result_count, knowledge_projection_version, "
                " retrieval_policy_version, retrieval_duration_ms, assignment, created_at) "
                "VALUES ('sess-m7', 'run-m7', 'rest api 403', 'fp-m7', 'wordpress', "
                "'GUIDANCE', 3, 1, 1, '1', 12, 'NONE', '2026-09-20 09:02:00.000000')"
            )
            connection.execute(
                "INSERT INTO experience_usage "
                "(id, retrieval_session_id, experience_id, rank, role, retrieval_score, "
                " retrieved_at, usage_signal, utility_label, created_at, updated_at) "
                "VALUES ('use-m7', 'sess-m7', 'exp-m7', 1, 'GUIDANCE', 0.8, "
                "'2026-09-20 09:02:00.000000', 'ADOPTED', 'HELPFUL', "
                "'2026-09-20 09:02:00.000000', '2026-09-20 09:02:00.000000')"
            )
            connection.commit()

    def test_the_database_starts_at_the_previous_revision(self, tmp_path: Path) -> None:
        db = tmp_path / "m7.db"
        self.build_milestone_eight_predecessor(db)

        assert current_revision(db) == "0005"
        assert "adapter_sessions" not in table_names(db)
        assert "adapter_events" not in table_names(db)

    def test_upgrading_reaches_head_in_place(self, tmp_path: Path) -> None:
        db = tmp_path / "m7-upgrade.db"
        self.build_milestone_eight_predecessor(db)

        upgrade_to_head(db)

        assert current_revision(db) == head_revision() == HEAD_REVISION
        assert table_names(db) == EXPECTED_TABLES

    def test_usage_survives_and_the_adapter_tables_arrive_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "m7-data.db"
        self.build_milestone_eight_predecessor(db)

        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            usage = connection.execute(
                "SELECT experience_id, usage_signal, utility_label "
                "FROM experience_usage WHERE id = 'use-m7'"
            ).fetchone()
            sessions = connection.execute("SELECT COUNT(*) FROM adapter_sessions").fetchone()
            events = connection.execute("SELECT COUNT(*) FROM adapter_events").fetchone()

        assert usage == ("exp-m7", "ADOPTED", "HELPFUL")
        assert sessions[0] == 0
        assert events[0] == 0

    def test_an_adapter_session_can_be_recorded_after_the_upgrade(self, tmp_path: Path) -> None:
        """The new tables have to be usable against runs the migration did not create."""
        db = tmp_path / "m7-usable.db"
        self.build_milestone_eight_predecessor(db)
        upgrade_to_head(db)

        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO adapter_sessions "
                "(id, provider, external_session_id, adapter_name, aer_run_id, "
                " protocol_version, created_at, updated_at) "
                "VALUES ('as-1', 'openai', 'conv-1', 'aer-codex', 'run-m7', '1', "
                "'2026-09-20 10:00:00.000000', '2026-09-20 10:00:00.000000')"
            )
            connection.execute(
                "INSERT INTO adapter_events "
                "(id, provider, external_event_id, adapter_name, event_type, aer_run_id, "
                " applied, created_at) "
                "VALUES ('ae-1', 'openai', 'evt-1', 'aer-codex', 'TOOL_CALL', 'run-m7', "
                " 1, '2026-09-20 10:00:01.000000')"
            )
            connection.commit()
            stored = connection.execute(
                "SELECT adapter_name, event_type, applied FROM adapter_events"
            ).fetchone()

        assert stored == ("aer-codex", "TOOL_CALL", 1)


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
        assert "ON DELETE CASCADE" in schema["experience_usage"]
        # A retrieval outlives the run it informed: deleting the run must not delete
        # the evidence that a search happened (round-7 brief, section 24).
        assert "ON DELETE SET NULL" in schema["retrieval_sessions"]

    def test_the_adapter_keys_survive_generation(self, tmp_path: Path) -> None:
        """Both adapter constraints are the mechanism behind a stated guarantee."""
        db = tmp_path / "adapter-keys.db"
        upgrade_to_head(db)
        schema = read_schema(db)

        # A retried hook cannot produce a second AER event (section 18).
        assert "UNIQUE (provider, external_event_id)" in schema["adapter_events"]
        # A reconnecting Agent resumes the run it was reporting into (section 21).
        assert "UNIQUE (provider, external_session_id)" in schema["adapter_sessions"]

    def test_the_ledger_keeps_its_memory_when_an_event_is_pruned(self, tmp_path: Path) -> None:
        """``SET NULL``, not ``CASCADE``: forgetting the trace must not re-open dedup."""
        db = tmp_path / "adapter-event-fk.db"
        upgrade_to_head(db)

        assert "ON DELETE CASCADE" in read_schema(db)["adapter_sessions"]
        assert "ON DELETE SET NULL" in read_schema(db)["adapter_events"]

    def test_the_usage_uniqueness_survives_generation(self, tmp_path: Path) -> None:
        """One experience appears at most once in one retrieval result.

        The composite UNIQUE is what turns "the retriever returned the same record
        twice" into a loud failure instead of a doubled statistic (section 49), so it
        is asserted on the generated schema rather than trusted to the model.
        """
        db = tmp_path / "usage-unique.db"
        upgrade_to_head(db)

        assert "UNIQUE (retrieval_session_id, experience_id)" in read_schema(db)["experience_usage"]

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
            "retrieval_sessions",
            "experience_usage",
            "adapter_sessions",
            "adapter_events",
        }
        assert table_names(db) == EXPECTED_TABLES
        assert ErrorRow.__tablename__ == "errors"
        assert RecoveryRow.__tablename__ == "recoveries"
        assert VerificationRow.__tablename__ == "verifications"
        assert ExperienceRow.__tablename__ == "experiences"
        assert ExperienceSourceRow.__tablename__ == "experience_sources"
        assert RetrievalSessionRow.__tablename__ == "retrieval_sessions"
        assert ExperienceUsageRow.__tablename__ == "experience_usage"
        assert AdapterSessionRow.__tablename__ == "adapter_sessions"
        assert AdapterEventRow.__tablename__ == "adapter_events"
