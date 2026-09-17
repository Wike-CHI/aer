#!/usr/bin/env python3
"""Restore an AER SQLite database from a backup produced by ``backup_sqlite.py``.

This is the most dangerous command in the repository, so it is built to be
annoying on purpose:

* **both** ``--source-backup`` and ``--target`` are required. There is no
  "restore the latest" shortcut -- choosing a recovery point is an operator
  decision, and the script must not guess it from a directory listing;
* the backup is verified with ``PRAGMA integrity_check`` *before* the target is
  touched, because discovering a corrupt backup after overwriting production is
  the one outcome that cannot be recovered from;
* an existing target is not overwritten without ``--force``;
* stale ``-wal`` / ``-shm`` files beside the target are removed first. This one
  matters: AER runs SQLite in WAL mode, and a leftover write-ahead log belonging
  to the *previous* database will be replayed over the freshly restored file at
  the next open, producing silent corruption. Deleting them is not optional.

Deliberately absent: automatic restore, automatic ``alembic downgrade``, and any
coupling to the image rollback path. Reverting the *application* and reverting the
*database* are separate decisions with separate risk profiles (D-044).

::

    python /app/scripts/restore_sqlite.py \\
        --source-backup /backups/aer-20260916-143000-a81d92f.db \\
        --target /data/aer.db --force
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# `scripts/` is deliberately not a Python package: these are operator entry points
# invoked as files, not importable modules (the repository layout in README.md
# says so, and Docker copies them as files). So a sibling import needs the script's
# own directory on the path -- which `python scripts/restore_sqlite.py` does not
# provide, because sys.path[0] becomes the *script* directory only when the script
# is a package entry point. Reusing the backup helpers rather than copying them is
# the lesser evil: one implementation of "verify before you trust it".
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_sqlite import (
    BackupError,
    clear_wal_sidecars,
    open_read_only,
    require_intact,
    wal_sidecars,
)


class RestoreError(RuntimeError):
    """The restore was refused or failed. The target is left untouched when refused."""


def _require_intact(path: str | Path, *, label: str) -> str:
    """Verify ``path``, reporting failure as :class:`RestoreError`.

    ``backup_sqlite.require_intact`` raises ``BackupError``; translating it keeps
    this module's contract single-typed ("anything that goes wrong is a
    RestoreError"), so callers -- including the CLI and the tests -- never have to
    know which helper ran under the hood. The original error is preserved as
    ``__cause__``.
    """
    try:
        return require_intact(path, label=label)
    except BackupError as exc:
        raise RestoreError(str(exc)) from exc


def clear_write_ahead_logs(db_path: str | Path) -> list[Path]:
    """Remove stale ``-wal`` / ``-shm`` files beside ``db_path``.

    The mechanics live in :func:`backup_sqlite.clear_wal_sidecars` -- one
    implementation, shared, because "these files must not outlive the database" is
    a single rule. Only the error type differs: this path is safety-critical during
    a restore, so a failure is translated into :class:`RestoreError` instead of the
    backup module's ``BackupError``.
    """
    try:
        return clear_wal_sidecars(db_path)
    except BackupError as exc:
        raise RestoreError(str(exc)) from exc


def restore_backup(
    source_backup: str | Path, target: str | Path, *, force: bool = False
) -> tuple[Path, str]:
    """Restore ``source_backup`` onto ``target`` using the SQLite backup API.

    Returns ``(destination, integrity_verdict)``. The verdict is returned rather
    than recomputed by the caller on purpose: recomputing it means opening the
    restored file once more *after* the ``-wal``/``-shm`` cleanup has run, which
    recreates the very sidecars that cleanup exists to remove. A
    disaster-recovery drill caught exactly that, so the last open now happens
    here, immediately before the last cleanup.

    Raises:
        RestoreError: the request was refused (missing/invalid arguments, existing
            target without ``force``) or the copy failed. A refused restore never
            modifies the target.
    """
    backup = Path(source_backup)
    destination = Path(target)

    if not backup.is_file():
        raise RestoreError(f"Backup does not exist: {backup.as_posix()}")
    if backup.resolve() == destination.resolve():
        raise RestoreError("Source and target are the same file; refusing to restore onto itself.")
    if destination.exists() and not force:
        raise RestoreError(
            f"Target already exists: {destination.as_posix()}. "
            "Pass --force to overwrite it (the current file will be replaced)."
        )

    # Verify before touching anything: a corrupt backup must be a no-op.
    _require_intact(backup, label="backup")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Remembered so a failed copy can undo the one thing it may have created. A
    # zero-length `aer.db` left where the target should be looks like a database and
    # is the worst possible outcome to debug at 3am.
    target_was_absent = not destination.exists()
    # Must happen *before* opening the target, or the old WAL is replayed over the
    # restored pages (see the module docstring).
    clear_write_ahead_logs(destination)

    # `closing`, not `with`: a sqlite3 connection used as a context manager manages
    # the *transaction*, not the connection. Leaving it open would keep a lock on
    # the database -- and the `-wal` beside it could then not be removed, which is
    # exactly the cleanup that has to happen before this function returns.
    try:
        with (
            # Read-only, through the same helper every other read path uses: the
            # source of a restore may live on a read-only filesystem, and a plain
            # `sqlite3.connect` there opens it read-write and fails.
            closing(open_read_only(backup)) as source_connection,
            closing(sqlite3.connect(destination, timeout=30.0)) as target_connection,
        ):
            source_connection.backup(target_connection)
    except sqlite3.Error as exc:
        if target_was_absent:
            # Leave the filesystem as we found it. `missing_ok` because the failure
            # may have happened before the file was created at all.
            Path(destination).unlink(missing_ok=True)
            clear_write_ahead_logs(destination)
        raise RestoreError(f"Restoring {backup.as_posix()} failed: {exc}") from exc

    # Both handles are closed by now: SQLite checkpoints and drops the WAL on its
    # final close, and anything it left behind is removed so the next process cannot
    # replay a write-ahead log that belongs to the database we replaced.
    clear_write_ahead_logs(destination)

    verdict = _require_intact(destination, label="restored database")

    # ...and clear once more, this time *after* the last open. Verification opens
    # the restored file read-only, and a read-only connection cannot checkpoint, so
    # SQLite deliberately leaves the ``-wal``/``-shm`` it created behind. Clearing
    # before the last open would be undone by it -- which is why nothing may open
    # the target after this point.
    clear_write_ahead_logs(destination)
    return destination, verdict


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="restore_sqlite.py",
        description="Restore an AER database from a backup. Both paths are mandatory.",
    )
    parser.add_argument("--source-backup", type=Path, required=True, help="backup file to restore")
    parser.add_argument("--target", type=Path, required=True, help="database file to replace")
    parser.add_argument("--force", action="store_true", help="overwrite an existing target")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify the backup and report, but do not write the target",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)

    def log(message: str) -> None:
        print(f"[restore] {message}", file=sys.stderr, flush=True)

    try:
        source_integrity = _require_intact(args.source_backup, label="backup")
        log(f"backup verified (integrity_check={source_integrity})")

        pre_existing = wal_sidecars(args.target)
        if pre_existing:
            log(f"stale write-ahead logs beside target: {[p.name for p in pre_existing]}")

        if args.dry_run:
            log("dry run: target left untouched")
            report: dict[str, object] = {
                "dry_run": True,
                "source_backup": args.source_backup.as_posix(),
                "target": args.target.as_posix(),
                "backup_integrity": source_integrity,
                "stale_wal_files": [p.name for p in pre_existing],
                "target_exists": args.target.exists(),
            }
        else:
            _, target_integrity = restore_backup(args.source_backup, args.target, force=args.force)
            log(f"restored {args.source_backup.as_posix()} -> {args.target.as_posix()}")
            report = {
                "dry_run": False,
                "source_backup": args.source_backup.as_posix(),
                "target": args.target.as_posix(),
                "backup_integrity": source_integrity,
                # Taken from the restore itself: re-checking here would open the
                # target again and recreate the write-ahead files just removed.
                "target_integrity": target_integrity,
                "stale_wal_files": [p.name for p in pre_existing],
                "target_bytes": args.target.stat().st_size,
            }
    except (RestoreError, BackupError) as exc:
        print(f"[restore] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError as exc:
        # An operator script must never end in a bare traceback: the exit code is
        # what the deployment pipeline reads, and an unhandled exception here is
        # indistinguishable from a crash in the tooling itself.
        print(f"[restore] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    print(
        "[restore] next: ensure no process is holding the database open, then run the "
        "deployment that matches this backup's alembic_revision",
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
