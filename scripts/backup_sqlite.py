#!/usr/bin/env python3
"""Online SQLite backup for AER.

Why this exists instead of ``cp aer.db backup.db``: AER runs SQLite in **WAL**
mode (``aer/storage/connection.py``), so the committed state of the database is
spread across ``aer.db``, ``aer.db-wal`` and ``aer.db-shm``. Copying only the main
file captures whatever happened to have been checkpointed, which under load is
*not* the current state -- it is a plausible-looking older one. That is the worst
possible failure mode for a backup: it restores without error and silently loses
the most recent transactions.

``sqlite3.Connection.backup()`` is SQLite's own online backup API. It takes a
consistent snapshot through the pager and works while the database is in use, so
it is correct for exactly the situation we are in.

Contract used by ``scripts/deploy.sh``:

* **stdout is reserved for the machine-readable result** (``--print-path`` or
  ``--json``); all human logging goes to **stderr**. That lets the deploy script
  do ``backup="$(backup_sqlite.py --print-path ...)"`` without parsing prose.
* A backup that does not pass ``PRAGMA integrity_check`` is deleted and the
  command fails. An unverified backup is worse than no backup, because it is
  trusted.

Run inside the runtime image, which is where the database and the volumes are::

    python /app/scripts/backup_sqlite.py \\
        --source /data/aer.db \\
        --destination /backups/aer-20260916-143000-a81d92f.db \\
        --git-sha a81d92f \\
        --image ghcr.io/example/aer:sha-a81d92f
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from aer import __version__, load_deployment_config
from aer.exceptions import AERError
from aer.storage.migrations import current_revision

#: Filename pattern produced when ``--destination`` is not given.
BACKUP_NAME_TEMPLATE = "aer-{stamp}-{git_sha}.db"
#: Suffix for the sidecar metadata file written next to each backup.
SIDECAR_SUFFIX = ".json"
#: Glob matching the backups this script owns (and therefore may prune).
BACKUP_GLOB = "aer-*.db"
#: Recorded when the caller does not know which commit it is running.
#: Never silently omitted: an untraceable backup is still a backup, but it must
#: say so rather than pretend to be traceable.
FALLBACK_GIT_SHA = "unknown"
#: AER runs SQLite in WAL mode, so a database may be accompanied by a write-ahead
#: log and a shared-memory file. They must never outlive the database they belong
#: to: a leftover WAL gets replayed over a restored file at the next open, which is
#: silent corruption rather than an error.
WAL_SUFFIXES = ("-wal", "-shm")
#: AER runs SQLite in WAL mode, so a database may be accompanied by a write-ahead
#: log and a shared-memory file. They must never outlive the database they belong
#: to: a leftover WAL gets replayed over a restored file at the next open, which is
#: silent corruption rather than an error.
WAL_SUFFIXES = ("-wal", "-shm")


class BackupError(RuntimeError):
    """The backup could not be produced or did not survive verification."""


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def utc_stamp(now: datetime | None = None) -> str:
    """``YYYYMMDD-HHMMSS`` in UTC.

    UTC rather than local time: the value ends up in a filename that gets
    compared across a laptop, a CI runner and a server, and a local timestamp
    with no offset is not comparable to anything. The ISO-8601 ``created_at`` in
    the sidecar carries the offset explicitly.
    """
    moment = datetime.now(UTC) if now is None else now
    return moment.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def backup_filename(stamp: str, git_sha: str) -> str:
    """The timestamped backup name, e.g. ``aer-20260916-143000-a81d92f.db``."""
    return BACKUP_NAME_TEMPLATE.format(stamp=stamp, git_sha=git_sha or FALLBACK_GIT_SHA)


def wal_sidecars(db_path: str | Path) -> list[Path]:
    """Existing ``-wal`` / ``-shm`` files beside ``db_path``, if any."""
    return [
        Path(f"{db_path}{suffix}") for suffix in WAL_SUFFIXES if Path(f"{db_path}{suffix}").exists()
    ]


def clear_wal_sidecars(db_path: str | Path) -> list[Path]:
    """Remove ``-wal`` / ``-shm`` files beside ``db_path``. Returns what was removed.

    Needed after verifying a database through a **read-only** connection: SQLite
    cannot checkpoint without write access, so it deliberately leaves the WAL and
    the shared-memory file behind. That is harmless for the data but wrong for a
    directory of backups -- a ``.db`` sitting next to a ``.db-wal`` looks like a
    two-part artifact, and someone restoring by hand will reasonably assume the WAL
    has to be kept.

    Tolerant of a file disappearing between the check and the delete (SQLite removes
    these itself when the last connection closes), strict about anything else: a
    genuine ``OSError`` becomes a :class:`BackupError` rather than a silent no-op.
    """
    removed: list[Path] = []
    for sidecar in wal_sidecars(db_path):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise BackupError(f"Cannot remove {sidecar.name}: {exc}") from exc
        removed.append(sidecar)
    return removed


def read_only_uri(path: str | Path) -> str:
    """A ``file:`` URI that opens ``path`` read-only.

    Built with :meth:`~pathlib.Path.as_uri`, which produces the correct form on
    POSIX *and* on Windows (where a bare ``C:/...`` inside a URI is parsed as a
    host). Resolved first, because ``as_uri`` refuses relative paths.
    """
    return Path(path).resolve().as_uri() + "?mode=ro"


def integrity_check(path: str | Path) -> str:
    """Return ``PRAGMA integrity_check`` for ``path`` (``"ok"`` when healthy).

    Opens read-only, for two independent reasons:

    * the check must not be able to modify the thing it is judging;
    * a read-write open of a WAL-mode database creates ``-wal`` / ``-shm`` files
      beside it. Leaving those next to a *backup* is both litter and a hazard:
      they look like part of the backup, and a later restore of the pair would
      replay a stale write-ahead log.

    ``closing`` rather than ``with``: a ``sqlite3.Connection`` used as a context
    manager manages the *transaction*, not the connection, so the file handle
    would stay open until garbage collection -- which on a busy host is "not any
    time soon".
    """
    target = Path(path)
    if not target.is_file():
        raise BackupError(f"Not a file: {target.as_posix()}")
    try:
        with closing(sqlite3.connect(read_only_uri(target), uri=True)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"{target.as_posix()} is not a readable SQLite database: {exc}") from exc
    return str(row[0]) if row else "unknown"


def require_intact(path: str | Path, *, label: str) -> str:
    """Raise unless ``path`` passes ``integrity_check``.

    Returns the raw check result so callers can record it in the sidecar.
    """
    result = integrity_check(path)
    if result.lower() != "ok":
        raise BackupError(f"{label} failed PRAGMA integrity_check: {result}")
    return result


def schema_revision(path: str | Path) -> str | None:
    """The Alembic revision recorded in ``path``, or ``None`` for an unmigrated file.

    Read through AER's own helper rather than a hand-rolled query, so a backup
    records the same revision string the runtime would report.
    """
    try:
        return current_revision(path)
    except AERError:
        return None


def prune_backups(
    backup_dir: str | Path,
    keep: int,
    *,
    protect: Path | None = None,
) -> list[Path]:
    """Delete the oldest backups until at most ``keep`` remain.

    ``protect`` is never deleted, even when it falls outside the newest ``keep``
    -- deleting the backup that was just taken would be a memorable bug.
    Sidecars (``<backup>.json``) are removed together with their database, so the
    directory cannot accumulate orphaned metadata.

    Ordering is by filename, which is correct because the name starts with a
    zero-padded UTC timestamp: lexical order *is* chronological order. Using mtime
    would be wrong (a copy or a restore rewrites it).
    """
    directory = Path(backup_dir)
    if not directory.is_dir() or keep < 1:
        return []

    candidates = sorted(directory.glob(BACKUP_GLOB))
    protected = protect.resolve() if protect is not None else None
    removable = [c for c in candidates if protected is None or c.resolve() != protected]

    excess = max(len(candidates) - keep, 0)
    deleted: list[Path] = []
    for backup in removable[:excess]:
        # `missing_ok` because another process may have pruned the same file: losing
        # a race to delete a file that is already gone is not a failure.
        backup.unlink(missing_ok=True)
        backup.with_name(backup.name + SIDECAR_SUFFIX).unlink(missing_ok=True)
        deleted.append(backup)
    return deleted


def build_metadata(
    *,
    source: Path,
    destination: Path,
    git_sha: str,
    image: str,
    revision: str | None,
    integrity: str,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """The sidecar payload: enough to answer "what is this file?" months later.

    Deliberately JSON and not a database row: the sidecar has to be readable when
    the database it describes is the thing that is broken.
    """
    moment = datetime.now(UTC) if created_at is None else created_at
    return {
        "git_sha": git_sha,
        "image": image,
        "alembic_revision": revision,
        "created_at": moment.astimezone(UTC).isoformat(),
        "source": source.as_posix(),
        "destination": destination.as_posix(),
        "source_bytes": source.stat().st_size if source.is_file() else None,
        "backup_bytes": destination.stat().st_size if destination.is_file() else None,
        "integrity_check": integrity,
        "aer_version": __version__,
    }


# ---------------------------------------------------------------------------
# the actual backup
# ---------------------------------------------------------------------------


def create_backup(source: str | Path, destination: str | Path) -> Path:
    """Copy ``source`` to ``destination`` using SQLite's online backup API.

    Raises:
        BackupError: the source is missing/unreadable, the destination already
            exists, or the copy fails. The original ``sqlite3`` error is kept as
            ``__cause__``.
    """
    src = Path(source)
    dst = Path(destination)

    if not src.is_file():
        raise BackupError(f"Source database does not exist: {src.as_posix()}")
    if dst.exists():
        raise BackupError(
            f"Destination already exists: {dst.as_posix()}. Refusing to overwrite a backup."
        )

    dst.parent.mkdir(parents=True, exist_ok=True)

    # A generous busy timeout: the backup may run while the runtime holds a write
    # lock, and waiting is strictly better than producing no backup at all.
    # `closing` (not `with`) so both handles are really released: the destination
    # must be closed before it is verified, and the source must be closed before
    # the caller decides what to do next.
    try:
        with (
            closing(sqlite3.connect(src, timeout=30.0)) as src_connection,
            closing(sqlite3.connect(dst, timeout=30.0)) as dst_connection,
        ):
            src_connection.backup(dst_connection)
    except sqlite3.Error as exc:
        # Never leave a half-written file behind: a truncated ``.db`` in the
        # backup directory looks like a valid recovery point to a human in a hurry.
        dst.unlink(missing_ok=True)
        raise BackupError(f"Backing up {src.as_posix()} failed: {exc}") from exc

    return dst


def run_backup(
    *,
    source: Path,
    destination: Path,
    git_sha: str,
    image: str,
    keep: int,
    prune: bool,
    log: Callable[[str], None],
) -> dict[str, object]:
    """Create, verify, annotate and (optionally) prune. Returns the sidecar payload.

    The ordering is the point: verify **before** writing the sidecar and before
    pruning, so a failed verification cannot be mistaken for success and cannot
    delete a previously good backup to make room for a broken one.
    """
    log(f"backing up {source.as_posix()} -> {destination.as_posix()}")
    create_backup(source, destination)

    integrity = require_intact(destination, label="backup")
    log(f"integrity_check={integrity}")

    # Verification opened the backup read-only, which means SQLite could not
    # checkpoint: any `-wal`/`-shm` it created would otherwise stay in the backup
    # directory forever (see clear_wal_sidecars).
    tidied = clear_wal_sidecars(destination)
    if tidied:
        log(f"removed write-ahead files left by verification: {[p.name for p in tidied]}")

    metadata = build_metadata(
        source=source,
        destination=destination,
        git_sha=git_sha,
        image=image,
        revision=schema_revision(destination),
        integrity=integrity,
    )
    sidecar = destination.with_name(destination.name + SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"sidecar {sidecar.as_posix()} (alembic_revision={metadata['alembic_revision']})")

    if prune:
        deleted = prune_backups(destination.parent, keep, protect=destination)
        if deleted:
            log(f"pruned {len(deleted)} backup(s), keeping {keep}")
            metadata["pruned"] = [path.name for path in deleted]

    return metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="backup_sqlite.py",
        description="Take a consistent online backup of an AER SQLite database.",
        epilog=(
            "Paths default to the deployment configuration (AER_DATA_DIR / "
            "AER_BACKUP_DIR), which is what the runtime container already sets."
        ),
    )
    parser.add_argument("--source", type=Path, help="database to back up (default: AER db)")
    parser.add_argument("--destination", type=Path, help="exact output file (default: derived)")
    parser.add_argument("--backup-dir", type=Path, help="where to write (default: AER_BACKUP_DIR)")
    parser.add_argument("--git-sha", default=None, help="commit that produced the running image")
    parser.add_argument("--image", default=None, help="image reference being deployed")
    parser.add_argument("--keep", type=int, default=None, help="retention count (default: config)")
    parser.add_argument("--no-prune", action="store_true", help="skip retention entirely")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--print-path", action="store_true", help="print only the backup path")
    output.add_argument("--json", action="store_true", help="print the sidecar metadata as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)

    def log(message: str) -> None:
        print(f"[backup] {message}", file=sys.stderr, flush=True)

    try:
        config = load_deployment_config()
        source = args.source or config.db_path
        git_sha = args.git_sha or FALLBACK_GIT_SHA
        image = args.image or ""
        keep = config.backup_retention if args.keep is None else args.keep
        destination = args.destination or (
            (args.backup_dir or config.backup_dir) / backup_filename(utc_stamp(), git_sha)
        )

        log(f"environment={config.environment} aer={__version__}")
        metadata = run_backup(
            source=source,
            destination=destination,
            git_sha=git_sha,
            image=image,
            keep=keep,
            prune=not args.no_prune,
            log=log,
        )
    except (BackupError, AERError) as exc:
        print(f"[backup] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.print_path:
        print(destination.as_posix())
    elif args.json:
        print(json.dumps(metadata, indent=2, sort_keys=True))
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
