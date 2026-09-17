"""Tests for the production smoke test (sections 43, 44, 45).

The smoke test has one job -- decide whether a release may be called deployed --
and one rule that must never bend: it may read production data and must not write
to it. Both are asserted here, including the "nothing actually changed" half, which
is the part that is easy to get wrong and invisible when it is.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from aer.storage.migrations import current_revision, head_revision
from tests.infra.support import row_count, seed_runs


@pytest.fixture
def production(ops_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A seeded store plus an environment that points the smoke test at it."""
    seed_runs(ops_root / "data", ("smoke seed",))
    monkeypatch.setenv("AER_ENV", "production")
    monkeypatch.setenv("AER_DATA_DIR", str(ops_root / "data"))
    monkeypatch.setenv("AER_ARTIFACT_DIR", str(ops_root / "artifacts"))
    monkeypatch.setenv("AER_KNOWLEDGE_DIR", str(ops_root / "knowledge"))
    monkeypatch.setenv("AER_BACKUP_DIR", str(ops_root / "backups"))
    monkeypatch.delenv("AER_DB_PATH", raising=False)
    return ops_root


def run(smoke_test: ModuleType, *args: str) -> int:
    """Invoke the script's entry point."""
    return int(smoke_test.main(list(args)))


class TestProductionChecks:
    def test_a_healthy_deployment_passes(self, smoke_test: ModuleType, production: Path) -> None:
        assert run(smoke_test) == 0

    def test_it_reads_production_data_without_changing_it(
        self, smoke_test: ModuleType, production: Path
    ) -> None:
        """Reading is the job; writing would make the smoke test part of the incident."""
        database = production / "data" / "aer.db"
        before = (row_count(database, "runs"), current_revision(database))

        assert run(smoke_test) == 0

        assert (row_count(database, "runs"), current_revision(database)) == before
        # The only file the smoke test may create in a data directory is its probe,
        # and that must be gone again.
        assert [p.name for p in (production / "data").iterdir()] == ["aer.db"]

    def test_a_missing_database_fails(
        self, smoke_test: ModuleType, ops_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AER_ENV", "production")
        monkeypatch.setenv("AER_DATA_DIR", str(ops_root / "empty"))
        monkeypatch.setenv("AER_ARTIFACT_DIR", str(ops_root / "artifacts"))
        monkeypatch.setenv("AER_KNOWLEDGE_DIR", str(ops_root / "knowledge"))
        monkeypatch.setenv("AER_BACKUP_DIR", str(ops_root / "backups"))
        monkeypatch.delenv("AER_DB_PATH", raising=False)

        assert run(smoke_test) == 1

    def test_the_image_revision_must_match_the_database(
        self, smoke_test: ModuleType, production: Path
    ) -> None:
        """The check that catches "this image is older than the schema" after a rollback."""
        assert run(smoke_test, "--expect-revision", "0001") == 1
        assert run(smoke_test, "--expect-revision", head_revision()) == 0

    def test_it_reports_every_check_it_ran(
        self, smoke_test: ModuleType, production: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(smoke_test) == 0

        out = capsys.readouterr().out
        for name in (
            "runtime.version",
            "config.resolve",
            "directories.writable",
            "database.integrity",
            "database.revision",
            "runtime.open",
        ):
            assert name in out, f"{name} was not reported"


class TestTemporaryDatabaseCheck:
    def test_it_does_not_touch_the_production_database(
        self, smoke_test: ModuleType, production: Path
    ) -> None:
        database = production / "data" / "aer.db"
        before = row_count(database, "runs")

        # This is the check that *writes*; it must do so in a throwaway database.
        assert run(smoke_test, "--temp-db-check") == 0

        assert row_count(database, "runs") == before

    def test_it_reports_the_roundtrip(
        self, smoke_test: ModuleType, production: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(smoke_test, "--temp-db-check") == 0

        assert "runtime.temp_roundtrip" in capsys.readouterr().out


class TestJsonOutput:
    def test_the_json_report_is_machine_readable(
        self, smoke_test: ModuleType, production: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        assert run(smoke_test, "--json") == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["environment"] == "production"
        assert {check["name"] for check in payload["checks"]} >= {
            "database.integrity",
            "database.revision",
            "runtime.open",
        }


class TestDirectoryChecks:
    def test_the_write_probe_is_always_cleaned_up(
        self, smoke_test: ModuleType, tmp_path: Path
    ) -> None:
        target = tmp_path / "artifacts"

        result = smoke_test.check_directories_writable({"artifact": target})

        assert result.ok is True
        assert list(target.iterdir()) == []

    def test_an_unwritable_directory_is_reported_not_raised(
        self, smoke_test: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only volume must produce a failed check, not a traceback."""
        target = tmp_path / "readonly"
        target.mkdir()
        real_write_text = Path.write_text

        def deny(self: Path, *args: object, **kwargs: object) -> int:
            if self.name == smoke_test.WRITE_PROBE_NAME:
                raise OSError(13, "Permission denied")
            return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", deny)

        result = smoke_test.check_directories_writable({"data": target})

        assert result.ok is False
        assert "Permission denied" in result.detail
        assert list(target.iterdir()) == []


class TestSchemaCheck:
    def test_a_revision_the_image_does_not_know_is_explained(
        self, smoke_test: ModuleType, production: Path
    ) -> None:
        result = smoke_test.check_schema_revision(production / "data" / "aer.db", expected="0002")

        assert result.ok is False
        assert "0002" in result.detail


class TestTheSmokeTestNeverMigrates:
    """A check that promises not to modify what it judges must not migrate it.

    Found by the cross-revision recovery drill of 2026-09-17. `AER(...)` calls
    `upgrade_to_head` in its constructor, so opening a store is only a *read* while
    the database is already at head. On anything older -- exactly what a restored
    older backup is -- the open is a migration.

    The smoke test reported `database.revision` as failed and then upgraded the
    database from 0003 to 0004 anyway, which also destroyed the operator's only
    chance to see what revision they had actually restored.
    """

    @staticmethod
    def build_stale_store(data_dir: Path, revision: str) -> Path:
        """A store at ``revision``, built by Alembic, holding one real run.

        The row goes in as SQL on purpose: every higher-level helper in this suite
        goes through ``AER(...)``, and ``AER(...)`` migrates. Using one here would
        upgrade the database inside the fixture and leave the test asserting nothing.
        """
        from alembic import command

        from aer.storage.migrations import alembic_config

        data_dir.mkdir(parents=True, exist_ok=True)
        database = data_dir / "aer.db"
        command.upgrade(alembic_config(database), revision)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO runs (id, task_description, status, started_at) "
                "VALUES ('run-stale', 'pre-migration run', 'SUCCESS', "
                "'2026-09-16 09:00:00.000000')"
            )
            connection.commit()
        return database

    def test_a_stale_database_is_left_at_its_revision(
        self, smoke_test: ModuleType, ops_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = self.build_stale_store(ops_root / "data", "0003")
        monkeypatch.setenv("AER_ENV", "production")
        monkeypatch.setenv("AER_DATA_DIR", str(ops_root / "data"))
        monkeypatch.setenv("AER_ARTIFACT_DIR", str(ops_root / "artifacts"))
        monkeypatch.setenv("AER_KNOWLEDGE_DIR", str(ops_root / "knowledge"))
        monkeypatch.setenv("AER_BACKUP_DIR", str(ops_root / "backups"))
        monkeypatch.delenv("AER_DB_PATH", raising=False)
        assert current_revision(database) == "0003"

        assert run(smoke_test) == 1

        assert current_revision(database) == "0003", "the smoke test migrated the database"
        assert row_count(database, "runs") == 1
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "experiences" not in tables, "a new table appeared without a migration step"

    def test_the_unattempted_open_is_reported_rather_than_passed(
        self, smoke_test: ModuleType, ops_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was proven about the database, so the check must not read `ok`."""
        database = self.build_stale_store(ops_root / "data", "0003")

        revision = smoke_test.check_schema_revision(database, expected=head_revision())
        result = smoke_test._check_runtime_open(
            smoke_test.load_deployment_config({"AER_DATA_DIR": str(ops_root / "data")}),
            revision,
        )

        assert revision.ok is False
        assert result.ok is False
        assert result.name == "runtime.open"
        assert "not attempted" in result.detail
        assert current_revision(database) == "0003"

    def test_a_healthy_store_still_gets_opened(
        self, smoke_test: ModuleType, production: Path
    ) -> None:
        """The skip must apply only to the case that would migrate."""
        database = production / "data" / "aer.db"
        revision = smoke_test.check_schema_revision(database, expected=head_revision())
        # Resolved from the environment the fixture set, so the production path
        # rules (all paths absolute) are exercised rather than sidestepped.
        result = smoke_test._check_runtime_open(smoke_test.load_deployment_config(), revision)

        assert revision.ok is True
        assert result.ok is True
        assert "runs=" in result.detail
