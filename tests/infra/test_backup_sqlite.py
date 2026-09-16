"""Backup tests (Infrastructure milestone, section 69).

The properties under test are the ones that decide whether a backup is a recovery
point or a false sense of security: it must include committed data that is still
sitting in the write-ahead log, it must be verified before it is trusted, and
retention must never be able to delete the backup that was just taken.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from aer.storage.migrations import head_revision
from tests.infra.support import row_count, seed_runs, task_descriptions


class TestConsistentSnapshot:
    """Why ``sqlite3.Connection.backup()`` rather than copying the file."""

    def test_committed_rows_still_only_in_the_wal_reach_the_backup(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        from aer import AER

        with AER(data_dir) as runtime:
            runtime.start_run(task="committed, not yet checkpointed", task_type="seed").success()
            database = runtime.database.path
            wal = Path(f"{database}-wal")

            # The premise of the whole test: WAL mode is on and the committed row
            # has not been checkpointed into the main file yet. If this ever stops
            # holding, the test must fail loudly rather than quietly stop proving
            # anything.
            assert wal.is_file(), "expected a write-ahead log beside the database"
            assert wal.stat().st_size > 0, "expected uncommitted frames in the write-ahead log"

            destination = tmp_path / "backups" / "snapshot.db"
            backup_sqlite.create_backup(database, destination)

            # Read the backup while the source is still open, which is exactly the
            # situation a naive `cp aer.db backup.db` would get wrong.
            assert row_count(destination, "runs") == 1


class TestBackupFile:
    """Basic guarantees of the produced file."""

    def test_a_healthy_copy_is_produced(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one", "two"))
        destination = tmp_path / "backups" / "copy.db"

        backup_sqlite.create_backup(Path(data_dir) / "aer.db", destination)

        assert destination.is_file()
        assert backup_sqlite.integrity_check(destination) == "ok"
        assert task_descriptions(data_dir) == ["one", "two"]
        assert row_count(destination, "runs") == 2

    def test_verification_does_not_modify_the_backup(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        """Judging a file must not be able to change it."""
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "judged.db"
        backup_sqlite.create_backup(Path(data_dir) / "aer.db", destination)
        before = destination.read_bytes()

        assert backup_sqlite.integrity_check(destination) == "ok"
        # Called twice on purpose: the second call must be as harmless as the first.
        assert backup_sqlite.integrity_check(destination) == "ok"

        assert destination.read_bytes() == before

    def test_a_finished_backup_directory_holds_only_the_backup_and_its_sidecar(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        """No stray ``-wal``/``-shm``: a two-part backup invites a wrong manual restore.

        Read-only verification cannot checkpoint, so SQLite leaves those files
        behind on purpose; ``run_backup`` clears them, and this is the test that
        keeps that true.
        """
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "tidy.db"

        backup_sqlite.run_backup(
            source=Path(data_dir) / "aer.db",
            destination=destination,
            git_sha="abc1234",
            image="",
            keep=20,
            prune=False,
            log=lambda _: None,
        )

        assert sorted(p.name for p in destination.parent.iterdir()) == [
            destination.name,
            f"{destination.name}.json",
        ]

    def test_refuses_to_overwrite_an_existing_backup(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "existing.db"
        destination.parent.mkdir(parents=True)
        destination.write_text("previous recovery point", encoding="utf-8")

        with pytest.raises(backup_sqlite.BackupError, match="already exists"):
            backup_sqlite.create_backup(Path(data_dir) / "aer.db", destination)

        # The old file must be untouched: overwriting it would have destroyed the
        # only copy of a recovery point nobody had checked yet.
        assert destination.read_text(encoding="utf-8") == "previous recovery point"

    def test_refuses_a_missing_source(self, tmp_path: Path, backup_sqlite: ModuleType) -> None:
        with pytest.raises(backup_sqlite.BackupError, match="does not exist"):
            backup_sqlite.create_backup(tmp_path / "absent.db", tmp_path / "out.db")

    def test_a_file_that_is_not_a_database_fails_verification(
        self, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        bogus = tmp_path / "bogus.db"
        bogus.write_text("this is not a SQLite database", encoding="utf-8")

        with pytest.raises(backup_sqlite.BackupError):
            backup_sqlite.require_intact(bogus, label="backup")


class TestNamingAndMetadata:
    """A backup nobody can date or attribute is not much better than none."""

    def test_the_filename_carries_a_utc_timestamp_and_the_commit(
        self, backup_sqlite: ModuleType
    ) -> None:
        moment = datetime(2026, 9, 16, 14, 30, 0, tzinfo=UTC)

        name = backup_sqlite.backup_filename(backup_sqlite.utc_stamp(moment), "a81d92f")

        assert name == "aer-20260916-143000-a81d92f.db"

    def test_a_missing_commit_is_recorded_rather_than_omitted(
        self, backup_sqlite: ModuleType
    ) -> None:
        name = backup_sqlite.backup_filename("20260916-143000", "")

        assert name.endswith("-unknown.db"), name

    def test_the_sidecar_records_what_the_file_is(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "annotated.db"
        messages: list[str] = []

        metadata = backup_sqlite.run_backup(
            source=Path(data_dir) / "aer.db",
            destination=destination,
            git_sha="abc1234",
            image="ghcr.io/acme/aer:sha-abc1234",
            keep=20,
            prune=False,
            log=messages.append,
        )

        sidecar = destination.with_name(destination.name + ".json")
        assert sidecar.is_file()
        assert json.loads(sidecar.read_text(encoding="utf-8")) == metadata

        # The four fields a recovery decision actually needs.
        assert metadata["git_sha"] == "abc1234"
        assert metadata["image"] == "ghcr.io/acme/aer:sha-abc1234"
        assert metadata["alembic_revision"] == head_revision()
        assert isinstance(metadata["created_at"], str)
        assert metadata["integrity_check"] == "ok"
        assert metadata["backup_bytes"] > 0

    def test_the_revision_is_read_from_the_backup_not_the_source(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        """The sidecar must describe the file being restored, not the live store."""
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "revision.db"

        metadata = backup_sqlite.run_backup(
            source=Path(data_dir) / "aer.db",
            destination=destination,
            git_sha="abc1234",
            image="",
            keep=20,
            prune=False,
            log=lambda _: None,
        )

        assert backup_sqlite.schema_revision(destination) == metadata["alembic_revision"]


class TestRetention:
    """Simple retention, with the one rule that actually matters."""

    @staticmethod
    def make_backups(backup_sqlite: ModuleType, directory: Path, count: int) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        for index in range(count):
            backup = directory / f"aer-2026091{index}-000000-sha{index}.db"
            backup.write_text("db", encoding="utf-8")
            backup.with_name(backup.name + ".json").write_text("{}", encoding="utf-8")
            created.append(backup)
        return created

    def test_keeps_the_newest_and_deletes_the_rest_with_their_sidecars(
        self, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        directory = tmp_path / "backups"
        created = self.make_backups(backup_sqlite, directory, 5)

        deleted = backup_sqlite.prune_backups(directory, 2)

        assert deleted == created[:3]
        assert [p.name for p in sorted(directory.glob("aer-*.db"))] == [
            created[3].name,
            created[4].name,
        ]
        # No orphaned metadata: a sidecar without its database is misleading.
        assert sorted(p.name for p in directory.iterdir()) == sorted(
            [created[3].name, f"{created[3].name}.json", created[4].name, f"{created[4].name}.json"]
        )

    def test_never_deletes_the_backup_it_was_asked_to_protect(
        self, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        directory = tmp_path / "backups"
        created = self.make_backups(backup_sqlite, directory, 3)

        # `keep=1` would normally remove everything but the newest; the protected
        # file is the oldest here, on purpose.
        deleted = backup_sqlite.prune_backups(directory, 1, protect=created[0])

        assert created[0] not in deleted
        assert created[0].is_file()

    def test_is_a_no_op_for_a_directory_that_does_not_exist(
        self, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        assert backup_sqlite.prune_backups(tmp_path / "absent", 5) == []
        assert backup_sqlite.prune_backups(tmp_path, 0) == []


class TestCommandLine:
    """The contract deploy.sh depends on: stdout is machine-readable, only stdout."""

    def test_print_path_emits_only_the_path(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed_runs(data_dir, ("one",))
        destination = tmp_path / "backups" / "cli.db"

        exit_code = backup_sqlite.main(
            [
                "--source",
                str(Path(data_dir) / "aer.db"),
                "--destination",
                str(destination),
                "--git-sha",
                "abc1234",
                "--no-prune",
                "--print-path",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out.strip() == destination.as_posix()
        assert "backing up" in captured.err  # human logging, never on stdout

    def test_json_mode_reports_the_sidecar(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed_runs(data_dir, ("one",))

        exit_code = backup_sqlite.main(
            [
                "--source",
                str(Path(data_dir) / "aer.db"),
                "--backup-dir",
                str(tmp_path / "backups"),
                "--git-sha",
                "abc1234",
                "--no-prune",
                "--json",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["git_sha"] == "abc1234"
        assert Path(payload["destination"]).is_file()

    def test_a_failure_exits_non_zero_and_says_so(
        self, tmp_path: Path, backup_sqlite: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = backup_sqlite.main(
            ["--source", str(tmp_path / "absent.db"), "--destination", str(tmp_path / "out.db")]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "FAILED" in captured.err
        assert not (tmp_path / "out.db").exists()

    def test_retention_is_applied_by_the_command_line(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one",))
        backup_dir = tmp_path / "backups"

        for index in range(4):
            exit_code = backup_sqlite.main(
                [
                    "--source",
                    str(Path(data_dir) / "aer.db"),
                    "--destination",
                    str(backup_dir / f"aer-2026091{index}-000000-sha{index}.db"),
                    "--keep",
                    "2",
                ]
            )
            assert exit_code == 0

        assert len(sorted(backup_dir.glob("aer-*.db"))) == 2
