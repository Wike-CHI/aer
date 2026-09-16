"""Tests for the production smoke test (sections 43, 44, 45).

The smoke test has one job -- decide whether a release may be called deployed --
and one rule that must never bend: it may read production data and must not write
to it. Both are asserted here, including the "nothing actually changed" half, which
is the part that is easy to get wrong and invisible when it is.
"""

from __future__ import annotations

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
