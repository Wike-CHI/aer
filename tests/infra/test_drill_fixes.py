"""Regressions found by the disaster-recovery drill of 2026-09-17.

Both defects below were invisible to the existing suite, and each for a different
reason, which is worth recording:

* the restore left ``-shm``/``-wal`` beside the freshly restored database. The
  existing tests call :func:`restore_sqlite.restore_backup`, which *did* clean up
  correctly -- the extra open that recreated the files lived in ``main``. Only a
  CLI-level test can see a defect that exists in the gap between two functions.

* ``integrity_check`` could not read a WAL-flagged database from a read-only
  *filesystem*, which is exactly how a backup should be mounted during a recovery.
  Every existing test ran on a writable temp directory, so the read-only case was
  never exercised at all.

The second group fakes the mount rather than trying to create a read-only
filesystem, which is not portable across the platforms this repository builds on.
Faking the *failure mode* is enough: the decision the code makes about what to do
next is the thing under test.
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from types import ModuleType

import pytest

from tests.infra.support import seed_runs

#: What the kernel gives back when a read-only mount refuses to create ``-shm``.
READ_ONLY_MOUNT_ERROR = "unable to open database file"


def make_backup(backup_sqlite: ModuleType, data_dir: Path, destination: Path) -> Path:
    """Back up the store at ``data_dir`` and return the backup path."""
    backup_sqlite.create_backup(Path(data_dir) / "aer.db", destination)
    return destination


class _RefusingConnect:
    """Stands in for ``sqlite3.connect`` on a read-only filesystem.

    Refuses every read-only open *except* an immutable one, which is what SQLite
    does when it cannot create the ``-shm`` index. Records each attempt so a test
    can assert which URIs were tried.
    """

    def __init__(self, message: str = READ_ONLY_MOUNT_ERROR) -> None:
        self.message = message
        self.attempts: list[str] = []

    def __call__(self, database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        self.attempts.append(str(database))
        if "immutable=1" not in str(database):
            raise sqlite3.OperationalError(self.message)
        return sqlite3.connect(database, *args, **kwargs)  # type: ignore[arg-type]


class _RecordingConnect:
    """Delegates to the real ``sqlite3.connect`` and remembers every request."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __call__(self, database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        self.attempts.append(str(database))
        return sqlite3.connect(database, *args, **kwargs)  # type: ignore[arg-type]


class _FailingSourceConnection:
    """A source whose ``backup`` fails, as a disk dying mid-copy would.

    ``sqlite3.Connection`` is an immutable C type, so its ``backup`` method cannot be
    patched; the seam is the helper that creates the connection. That seam is worth
    having anyway -- it is the same one the read-only-mount fallback lives behind.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def backup(self, target: object) -> None:
        del target
        raise sqlite3.OperationalError("disk I/O error")

    def close(self) -> None:
        self._real.close()


@pytest.fixture
def failing_source(
    monkeypatch: pytest.MonkeyPatch, backup_sqlite: ModuleType, restore_sqlite: ModuleType
) -> None:
    """Make the restore's source connection fail when asked to copy."""

    def factory(path: object, **kwargs: object) -> _FailingSourceConnection:
        return _FailingSourceConnection(backup_sqlite.open_read_only(path, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(restore_sqlite, "open_read_only", factory)


@pytest.fixture
def faked_read_only_mount(
    monkeypatch: pytest.MonkeyPatch, backup_sqlite: ModuleType
) -> _RefusingConnect:
    """Replace ``backup_sqlite``'s view of sqlite3 with a read-only-mount stand-in."""
    shim = _RefusingConnect()
    monkeypatch.setattr(
        backup_sqlite,
        "sqlite3",
        types.SimpleNamespace(
            connect=shim,
            Error=sqlite3.Error,
            OperationalError=sqlite3.OperationalError,
        ),
    )
    return shim


class TestRestoreLeavesNoSidecars:
    """Defect 1: `main` re-opened the target after the cleanup had already run."""

    def test_the_cli_leaves_only_the_database_behind(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = tmp_path / "restored" / "aer.db"

        exit_code = restore_sqlite.main(
            ["--source-backup", str(backup), "--target", str(target), "--json"]
        )
        capsys.readouterr()

        assert exit_code == 0
        # A `-wal`/`-shm` next to a restored database is not litter: a write-ahead
        # log belonging to the *replaced* database would be replayed over the
        # restored pages at the next open. Nothing but the database may remain.
        assert sorted(path.name for path in target.parent.iterdir()) == ["aer.db"]

    def test_the_verdict_is_the_one_the_restore_verified(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
    ) -> None:
        """The caller must not re-check the target to learn what it already knows."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = tmp_path / "restored" / "aer.db"

        destination, verdict = restore_sqlite.restore_backup(backup, target)

        assert destination == target
        assert verdict == "ok"


class TestTheCopyAlsoSurvivesAReadOnlyMount:
    """The fallback has to be in every read path, not only in the check.

    Post-deploy verification on the real server caught this: with the first fix
    applied, verifying a backup on a read-only mount succeeded while the restore
    that used it still failed -- because only the integrity check had been fixed.
    A fix that covers one of two call sites is not a fix.
    """

    def test_the_source_is_opened_read_only(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "source.db")
        target = tmp_path / "restored" / "aer.db"

        recorder = _RecordingConnect()
        namespace = types.SimpleNamespace(
            connect=recorder,
            Error=sqlite3.Error,
            OperationalError=sqlite3.OperationalError,
        )
        monkeypatch.setattr(backup_sqlite, "sqlite3", namespace)
        monkeypatch.setattr(restore_sqlite, "sqlite3", namespace)

        restore_sqlite.restore_backup(backup, target)

        source_opens = [uri for uri in recorder.attempts if "source.db" in uri]
        assert source_opens, recorder.attempts
        assert all("mode=ro" in uri for uri in source_opens), source_opens

    def test_a_failed_copy_leaves_no_empty_database_behind(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        failing_source: None,
    ) -> None:
        """A zero-length `aer.db` looks like a database. Do not leave one behind."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = tmp_path / "restored" / "aer.db"

        with pytest.raises(restore_sqlite.RestoreError):
            restore_sqlite.restore_backup(backup, target)

        assert not target.exists()
        assert not target.parent.exists() or list(target.parent.iterdir()) == []

    def test_a_failed_copy_does_not_destroy_a_target_that_already_existed(
        self,
        data_dir: Path,
        tmp_path: Path,
        backup_sqlite: ModuleType,
        restore_sqlite: ModuleType,
        failing_source: None,
    ) -> None:
        """Removing "what we created" must never become removing what was there."""
        seed_runs(data_dir, ("one",))
        backup = make_backup(backup_sqlite, data_dir, tmp_path / "backups" / "one.db")
        target = Path(data_dir) / "aer.db"
        before = target.read_bytes()

        with pytest.raises(restore_sqlite.RestoreError):
            restore_sqlite.restore_backup(backup, target, force=True)

        assert target.exists()
        assert target.read_bytes() == before


class TestReadOnlyFilesystem:
    """Defect 2: a backup on a read-only mount could not be verified or restored."""

    def test_integrity_check_falls_back_to_immutable(
        self,
        data_dir: Path,
        backup_sqlite: ModuleType,
        faked_read_only_mount: _RefusingConnect,
    ) -> None:
        """A read-only mount must not make a healthy database unverifiable."""
        seed_runs(data_dir, ("one",))
        target = Path(data_dir) / "aer.db"

        assert backup_sqlite.integrity_check(target) == "ok"

        assert len(faked_read_only_mount.attempts) == 2, faked_read_only_mount.attempts
        assert "mode=ro" in faked_read_only_mount.attempts[0]
        assert "immutable=1" not in faked_read_only_mount.attempts[0]
        assert "immutable=1" in faked_read_only_mount.attempts[1]

    def test_a_non_empty_write_ahead_log_is_refused(
        self,
        data_dir: Path,
        backup_sqlite: ModuleType,
        faked_read_only_mount: _RefusingConnect,
    ) -> None:
        """The fallback must not silently ignore committed pages.

        ``immutable=1`` skips the write-ahead log by definition. If one exists and
        has content, the main file on its own is *not* the whole database, and a
        confident "ok" about stale pages would be worse than refusing to answer.
        """
        seed_runs(data_dir, ("one",))
        target = Path(data_dir) / "aer.db"
        Path(f"{target}-wal").write_bytes(b"committed pages the main file lacks")

        with pytest.raises(backup_sqlite.BackupError) as excinfo:
            backup_sqlite.integrity_check(target)

        assert "write-ahead log" in str(excinfo.value)
        assert len(faked_read_only_mount.attempts) == 1, "the fallback must not be taken"

    def test_an_empty_write_ahead_log_does_not_block_the_fallback(
        self,
        data_dir: Path,
        backup_sqlite: ModuleType,
        faked_read_only_mount: _RefusingConnect,
    ) -> None:
        """A zero-length `-wal` holds nothing, so ignoring it loses nothing.

        This is the shape SQLite leaves behind after a clean close on Windows, so
        treating it as a hazard would refuse the common case.
        """
        seed_runs(data_dir, ("one",))
        target = Path(data_dir) / "aer.db"
        Path(f"{target}-wal").write_bytes(b"")

        assert backup_sqlite.integrity_check(target) == "ok"
        assert len(faked_read_only_mount.attempts) == 2

    def test_an_unrelated_open_failure_is_not_mistaken_for_a_read_only_mount(
        self,
        data_dir: Path,
        backup_sqlite: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A different error must not be quietly retried with weaker guarantees."""
        seed_runs(data_dir, ("one",))
        target = Path(data_dir) / "aer.db"
        shim = _RefusingConnect(message="database disk image is malformed")
        monkeypatch.setattr(
            backup_sqlite,
            "sqlite3",
            types.SimpleNamespace(
                connect=shim,
                Error=sqlite3.Error,
                OperationalError=sqlite3.OperationalError,
            ),
        )

        with pytest.raises(backup_sqlite.BackupError) as excinfo:
            backup_sqlite.integrity_check(target)

        assert "malformed" in str(excinfo.value)
        assert len(shim.attempts) == 1
