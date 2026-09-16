"""Restore tests (Infrastructure milestone, section 70).

A restore is the one operation that can destroy a good database, so most of these
tests are about what the script *refuses* to do. The positive cases restore either
to a separate destination or onto a throwaway copy; the real development store is
never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import ModuleType

import pytest

from tests.infra.support import row_count, seed_runs, task_descriptions


def make_backup(backup_sqlite: ModuleType, data_dir: Path, destination: Path) -> Path:
    """Back up the store at ``data_dir`` and return the backup path."""
    backup_sqlite.create_backup(Path(data_dir) / "aer.db", destination)
    return destination


class TestRoundTrip:
    """The core promise: the old state comes back."""

    def test_restoring_to_a_separate_destination_recovers_the_earlier_state(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one", "two"))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "two.db")

        # The live store moves on after the backup...
        seed_runs(data_dir, ("three",))
        assert task_descriptions(data_dir) == ["one", "three", "two"]

        # ...and the backup still describes the moment it was taken.
        target = tmp_path / "restored" / "aer.db"
        restore_sqlite.restore_backup(backup, target)

        assert task_descriptions(target.parent) == ["one", "two"]
        # The live store is untouched by a restore into a different file.
        assert row_count(Path(data_dir) / "aer.db", "runs") == 3

    def test_restoring_onto_the_production_path_replaces_only_that_file(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one", "two"))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "two.db")
        seed_runs(data_dir, ("three",))

        restore_sqlite.restore_backup(backup, Path(data_dir) / "aer.db", force=True)

        assert task_descriptions(data_dir) == ["one", "two"]
        assert backup_sqlite.integrity_check(Path(data_dir) / "aer.db") == "ok"

    def test_stale_write_ahead_logs_beside_the_target_are_removed(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        """A leftover WAL belongs to the database being replaced, not the restored one."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = Path(data_dir) / "aer.db"
        stale = [Path(f"{target}{suffix}") for suffix in ("-wal", "-shm")]
        for path in stale:
            path.write_bytes(b"stale frames from the previous database")
        assert restore_sqlite.wal_sidecars(target) == stale

        restore_sqlite.clear_write_ahead_logs(target)
        restore_sqlite.restore_backup(backup, target, force=True)

        assert not any(path.exists() for path in stale)
        assert backup_sqlite.integrity_check(target) == "ok"


class TestRefusals:
    """Every one of these is a state we never want to be able to reach."""

    def test_refuses_without_force_when_the_target_exists(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = Path(data_dir) / "aer.db"
        before = target.read_bytes()

        with pytest.raises(restore_sqlite.RestoreError, match="already exists"):
            restore_sqlite.restore_backup(backup, target)

        assert target.read_bytes() == before

    def test_refuses_a_missing_backup(self, tmp_path: Path, restore_sqlite: ModuleType) -> None:
        with pytest.raises(restore_sqlite.RestoreError, match="does not exist"):
            restore_sqlite.restore_backup(tmp_path / "absent.db", tmp_path / "target.db")

    def test_refuses_to_restore_a_file_onto_itself(
        self, tmp_path: Path, restore_sqlite: ModuleType
    ) -> None:
        backup = tmp_path / "backup.db"
        backup.write_bytes(b"anything")

        with pytest.raises(restore_sqlite.RestoreError, match="same file"):
            restore_sqlite.restore_backup(backup, backup, force=True)

    def test_a_corrupt_backup_leaves_the_target_byte_identical(
        self, data_dir: Path, tmp_path: Path, restore_sqlite: ModuleType
    ) -> None:
        """Verification happens *before* the target is touched -- the whole point."""
        seed_runs(data_dir, ("one",))
        target = Path(data_dir) / "aer.db"
        before = target.read_bytes()
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_text("not a database at all", encoding="utf-8")

        with pytest.raises(restore_sqlite.RestoreError):
            restore_sqlite.restore_backup(corrupt, target, force=True)

        assert target.read_bytes() == before

    def test_clearing_a_wal_file_that_vanished_is_not_an_error(
        self, tmp_path: Path, restore_sqlite: ModuleType
    ) -> None:
        """SQLite removes WAL files itself; losing that race must stay benign."""
        target = tmp_path / "aer.db"
        target.write_bytes(b"db")
        racy = Path(f"{target}-wal")
        racy.write_bytes(b"frames")

        assert restore_sqlite.clear_write_ahead_logs(target) == [racy]
        assert restore_sqlite.clear_write_ahead_logs(target) == []
        assert not racy.exists()


class TestCommandLine:
    def test_both_paths_are_mandatory(self, restore_sqlite: ModuleType) -> None:
        with pytest.raises(SystemExit) as excinfo:
            restore_sqlite.parse_args([])

        assert excinfo.value.code == 2

    def test_dry_run_verifies_but_writes_nothing(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed_runs(data_dir, ("one", "two"))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "dry.db")
        seed_runs(data_dir, ("three",))
        target = Path(data_dir) / "aer.db"
        before = target.read_bytes()

        exit_code = restore_sqlite.main(
            ["--source-backup", str(backup), "--target", str(target), "--dry-run", "--json"]
        )

        assert exit_code == 0
        assert target.read_bytes() == before
        assert '"dry_run": true' in capsys.readouterr().out

    def test_a_failed_restore_exits_non_zero(
        self, tmp_path: Path, restore_sqlite: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = restore_sqlite.main(
            ["--source-backup", str(tmp_path / "absent.db"), "--target", str(tmp_path / "t.db")]
        )

        assert exit_code == 1
        assert "FAILED" in capsys.readouterr().err

    def test_an_unexpected_os_error_is_reported_not_raised(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An ops script must end in an exit code, never in a traceback."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "perm.db")
        target = tmp_path / "readonly" / "aer.db"
        target.parent.mkdir()

        def deny(*args: object, **kwargs: object) -> None:
            raise OSError("simulated permission failure")

        monkeypatch.setattr(Path, "mkdir", deny)

        exit_code = restore_sqlite.main(
            ["--source-backup", str(backup), "--target", str(target), "--force"]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "FAILED" in captured.err
        assert "simulated permission failure" in captured.err


class TestRestoreLeavesNoRubbish:
    def test_a_restore_is_repeatable(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        """Running the same restore twice must be idempotent, not accumulate files."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "repeat.db")
        target = Path(data_dir) / "aer.db"

        for _ in range(2):
            restore_sqlite.restore_backup(backup, target, force=True)

        assert sorted(p.name for p in Path(data_dir).iterdir()) == ["aer.db"]
        assert task_descriptions(data_dir) == ["one"]

    def test_restoring_does_not_modify_the_backup(
        self, data_dir: Path, tmp_path: Path, backup_sqlite: ModuleType, restore_sqlite: ModuleType
    ) -> None:
        seed_runs(data_dir, ("one", "two"))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "immutable.db")
        digest_before = shutil.copy(backup, tmp_path / "copy-for-comparison").read_bytes()

        restore_sqlite.restore_backup(backup, tmp_path / "target" / "aer.db")

        assert backup.read_bytes() == digest_before
