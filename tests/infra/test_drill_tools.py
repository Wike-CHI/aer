"""Tests for the disaster-recovery drill tools.

These three scripts are what makes a drill evidence rather than assertion: they
read a database without being able to modify it, compare two files logically
rather than by checksum, and create the records a preservation check needs.
Their own properties are therefore worth fixing in tests:

* ``drill_facts`` must not be able to write. It points at the production database.
* ``drill_facts`` must discover tables from ``sqlite_master``, not from a list in
  its source, or a drill against an older revision would report a missing table
  as a fact about the data.
* ``drill_compare`` must call two files equal when their *contents* are equal even
  though their bytes are not -- SQLite's header change counter guarantees that
  case, and a checksum comparison would report a false alarm.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from tests.infra.support import connect, seed_runs


def run_json(module: ModuleType, argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict:
    """Invoke a tool's ``main`` and return its JSON output."""
    exit_code = module.main(["tool", *argv])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    return json.loads(captured.out)


class TestDrillFactsIsReadOnly:
    """The single non-negotiable property: it cannot modify what it inspects."""

    def test_inspecting_a_database_leaves_it_byte_identical(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one", "two"))
        target = data_dir / "aer.db"
        before = target.read_bytes()

        report = run_json(drill_facts, ["facts", str(target), "--immutable"], capsys)

        assert report["integrity_check"] == "ok"
        assert target.read_bytes() == before
        assert sorted(path.name for path in data_dir.iterdir()) == ["aer.db"]

    def test_it_reports_how_the_file_was_opened(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Evidence has to say how it was obtained, including the access mode."""
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))

        plain = run_json(drill_facts, ["facts", str(data_dir / "aer.db")], capsys)
        immutable = run_json(
            drill_facts, ["facts", str(data_dir / "aer.db"), "--immutable"], capsys
        )

        assert plain["open_mode"] == "mode=ro"
        assert immutable["open_mode"] == "mode=ro&immutable=1"


class TestDrillFactsIsSchemaAgnostic:
    """A drill may run against any revision, so the table list must be discovered."""

    def test_the_reported_tables_are_the_ones_in_the_file(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))

        report = run_json(drill_facts, ["facts", str(data_dir / "aer.db")], capsys)

        with connect(data_dir / "aer.db") as connection:
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert set(report["tables"]) == actual
        assert set(report["row_counts"]) == actual - {"alembic_version"}

    def test_row_counts_match_the_file(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one", "two", "three"))

        report = run_json(drill_facts, ["facts", str(data_dir / "aer.db")], capsys)

        assert report["row_counts"]["runs"] == 3
        assert report["alembic_revision"] is not None

    def test_an_absent_file_is_reported_rather_than_crashing(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = run_json(drill_facts, ["facts", str(tmp_path / "absent.db")], capsys)

        assert report == {
            "exists": False,
            "open_mode": "mode=ro",
            "path": (tmp_path / "absent.db").as_posix(),
        }

    def test_samples_read_every_column_of_one_row_per_table(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Field-level comparison is the point; row counts alone prove little."""
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))

        report = run_json(drill_facts, ["samples", str(data_dir / "aer.db")], capsys)

        run_sample = report["samples"]["runs"]
        assert run_sample["present"] is True
        assert run_sample["row"]["task_description"] == "one"
        with connect(data_dir / "aer.db") as connection:
            expected = {row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
        assert set(run_sample["columns"]) == expected

    def test_a_table_that_does_not_exist_is_reported_as_absent(
        self, tmp_path: Path, drill_facts: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))

        report = run_json(drill_facts, ["samples", str(data_dir / "aer.db")], capsys)

        with connect(data_dir / "aer.db") as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        for table, entry in report["samples"].items():
            assert entry["present"] is (table in tables)


class TestDrillCompare:
    """Distinguishing "same data" from "same bytes" is the whole job."""

    def test_two_logically_equal_files_are_equal_despite_differing_bytes(
        self,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        drill_compare: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A backup is not byte-identical to its source, and that is not a problem."""
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one", "two"))
        source = data_dir / "aer.db"
        copy = tmp_path / "backups" / "copy.db"
        backup_sqlite.create_backup(source, copy)

        report = run_json(
            drill_compare, [str(source), str(copy), "--a-immutable", "--b-immutable"], capsys
        )

        assert report["logical_equal"] is True
        assert report["logical"]["a"] == report["logical"]["b"]
        # The header's change counter is expected to differ; that is the reason a
        # checksum comparison cannot be used as the drill's evidence.
        assert report["header_change_counter"]["a"] != report["header_change_counter"]["b"]

    def test_a_changed_row_makes_the_files_logically_different(
        self,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        drill_compare: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Content is compared, so an added row must be visible."""
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))
        copy = tmp_path / "backups" / "copy.db"
        backup_sqlite.create_backup(data_dir / "aer.db", copy)
        seed_runs(data_dir, ("two",))

        report = run_json(
            drill_compare,
            [str(data_dir / "aer.db"), str(copy), "--a-immutable", "--b-immutable"],
            capsys,
        )

        assert report["logical_equal"] is False
        assert report["logical"]["a"]["contents"]["runs"]["rows"] == 2
        assert report["logical"]["b"]["contents"]["runs"]["rows"] == 1

    def test_an_identical_file_pair_is_reported_byte_identical(
        self, tmp_path: Path, drill_compare: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        seed_runs(data_dir, ("one",))
        twin = tmp_path / "twin.db"
        twin.write_bytes((data_dir / "aer.db").read_bytes())

        report = run_json(
            drill_compare, [str(data_dir / "aer.db"), str(twin), "--a-immutable"], capsys
        )

        assert report["byte_identical"] is True
        assert report["differing_byte_count"] == 0
        assert report["logical_equal"] is True


class TestSharedTablesMode:
    """For a cross-revision drill: what a migration must leave alone.

    Comparing everything would report "different" for a migration that did exactly
    what it promised, because a migration is *supposed* to add tables and bump the
    revision. Comparing only what both files have is the meaningful question.
    """

    @staticmethod
    def make_pair(tmp_path: Path, ddl_b: str) -> tuple[Path, Path]:
        """Two files: same table, same rows, different CREATE TABLE statement."""
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        for path, ddl in ((a, "CREATE TABLE t (id TEXT PRIMARY KEY, n INTEGER)"), (b, ddl_b)):
            with sqlite3.connect(path) as connection:
                connection.execute(ddl)
                connection.execute("INSERT INTO t VALUES ('row-1', 5)")
                connection.execute("INSERT INTO t VALUES ('row-2', 7)")
                connection.commit()
        return a, b

    def test_added_tables_do_not_make_the_shared_tables_different(
        self, tmp_path: Path, drill_compare: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        old, new = self.make_pair(tmp_path, "CREATE TABLE t (id TEXT PRIMARY KEY, n INTEGER)")
        with sqlite3.connect(new) as connection:
            connection.execute("CREATE TABLE added (id TEXT PRIMARY KEY)")
            connection.commit()

        full = run_json(drill_compare, [str(old), str(new)], capsys)
        shared = run_json(drill_compare, [str(old), str(new), "--shared-tables"], capsys)

        assert full["logical_equal"] is False
        assert shared["logical_equal"] is True
        assert shared["tables_only_in_b"] == ["added"]
        assert shared["tables_only_in_a"] == []

    def test_a_changed_table_definition_is_detected(
        self, tmp_path: Path, drill_compare: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The rows are byte-for-byte identical here; only the DDL differs.

        A comparison built on row digests alone would call this preserved, and it
        would be wrong: the column now has a default that the old schema did not.
        """
        old, new = self.make_pair(
            tmp_path, "CREATE TABLE t (id TEXT PRIMARY KEY, n INTEGER DEFAULT 0)"
        )

        shared = run_json(drill_compare, [str(old), str(new), "--shared-tables"], capsys)

        assert shared["logical"]["a"]["contents"] == shared["logical"]["b"]["contents"]
        assert shared["logical_equal"] is False


class TestDrillSeed:
    """The sandbox store that gives the preservation check something to preserve."""

    def test_it_requires_an_explicit_target(self, drill_seed: ModuleType) -> None:
        """There is no default: a seeder must never be able to guess at production."""
        assert drill_seed.main(["drill_seed.py"]) == 2

    def test_it_creates_one_of_every_substantive_record(
        self, tmp_path: Path, drill_seed: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "seeded"

        summary = run_json(drill_seed, [str(data_dir)], capsys)

        assert summary["run"]["status"] == "SUCCESS"
        assert summary["events"]["count"] >= 1
        assert summary["recoveries"]["count"] == 1
        assert summary["verification"]["passed"] is True
        assert summary["verified_success"] is True
        assert summary["experience"]["id"] is not None
        assert summary["experience_sources"]["count"] == 1

    def test_every_record_it_reports_is_actually_in_the_database(
        self, tmp_path: Path, drill_seed: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The summary is read back from storage, so it must match the file."""
        data_dir = tmp_path / "seeded"

        summary = run_json(drill_seed, [str(data_dir)], capsys)

        with sqlite3.connect(data_dir / "aer.db") as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE id = ?", (summary["run"]["id"],)
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id = ?", (summary["run"]["id"],)
                ).fetchone()[0]
                == summary["events"]["count"]
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM experiences WHERE id = ?",
                    (summary["experience"]["id"],),
                ).fetchone()[0]
                == 1
            )
