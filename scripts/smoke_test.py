#!/usr/bin/env python3
"""Production smoke test for AER.

Named ``smoke_test`` and not ``healthcheck`` on purpose: AER is an embedded
runtime with no HTTP surface, so there is nothing to poll and no endpoint that
could return "healthy". What can be checked is that **this image, on this host,
pointed at these volumes, can open and use the real store** -- which is exactly
what a deployment needs to know before it is declared successful.

Two modes, one script:

* **production checks** (default) run against the configured database and are
  strictly read-only: existence, ``PRAGMA integrity_check``, schema revision, and
  a runtime open plus a repository read. The revision is compared *before* opening
  through :class:`~aer.runtime.runtime.AER`, because ``AER(...)`` applies pending
  migrations on construction -- checking first means the open is a no-op instead
  of a schema change.
* **``--temp-db-check``** additionally proves the write path end to end (create a
  schema, record a run, read it back) inside a throwaway directory. It is opt-in
  and never points at the production database: a smoke test that creates test runs
  in production data has manufactured the incident it was meant to prevent.

Exit code 0 means every check passed; 1 means at least one failed and the caller
must not promote the release.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aer import AER, __version__, load_deployment_config
from aer.config import DeploymentConfig
from aer.exceptions import AERError
from aer.storage.migrations import current_revision, head_revision

# `scripts/` is deliberately not a Python package (see README.md): these are
# operator entry points invoked as files, and Docker copies them as files. A
# sibling import therefore needs the script's own directory on the path. The
# alternative -- a second, subtly different implementation of "is this SQLite file
# healthy" -- is worse: the whole point of the check is that it is the *same*
# check the backup and restore paths use.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_sqlite import BackupError, integrity_check

#: Name of the file written and removed again to prove a directory is writable.
#: Dot-prefixed and always cleaned up: leaving litter in a data directory during a
#: smoke test is how a "verified" deployment ends up with junk in it.
WRITE_PROBE_NAME = ".aer-smoke-write-probe"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome."""

    name: str
    ok: bool
    detail: str


def check_directories_writable(directories: dict[str, Path]) -> CheckResult:
    """Verify every persistent directory exists and accepts a write.

    Runs as the container's non-root user, so this is the check that catches "the
    host directory is owned by root and the image runs as uid 10001" -- a failure
    that otherwise only shows up at the first real write, mid-deployment.
    """
    problems: list[str] = []
    for label, directory in directories.items():
        probe = directory / WRITE_PROBE_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe.write_text("smoke\n", encoding="utf-8")
        except OSError as exc:
            problems.append(f"{label}={directory.as_posix()} ({exc.strerror or exc})")
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                problems.append(f"{label}={directory.as_posix()} (probe not removable)")

    if problems:
        return CheckResult("directories.writable", False, "; ".join(problems))
    return CheckResult(
        "directories.writable",
        True,
        ", ".join(f"{label}={d.as_posix()}" for label, d in directories.items()),
    )


def check_database_integrity(db_path: str | Path) -> CheckResult:
    """Existence plus ``PRAGMA integrity_check``, both strictly read-only."""
    path = Path(db_path)
    if not path.is_file():
        return CheckResult("database.exists", False, f"missing: {path.as_posix()}")
    try:
        result = integrity_check(path)
    except BackupError as exc:
        return CheckResult("database.integrity", False, str(exc))
    return CheckResult(
        "database.integrity",
        result.lower() == "ok",
        f"{path.as_posix()} -> {result}",
    )


def check_schema_revision(db_path: str | Path, *, expected: str | None = None) -> CheckResult:
    """The database must already be at the head revision this image ships."""
    try:
        head = head_revision()
        current = current_revision(db_path)
    except AERError as exc:
        # Covers the classic packaging failure: the image installed AER but not
        # `alembic.ini`, so the migration history cannot even be located.
        return CheckResult("database.revision", False, f"cannot read revision: {exc}")

    wanted = head if expected is None else expected
    if current != wanted:
        return CheckResult(
            "database.revision",
            False,
            f"database is at {current!r}, image expects {wanted!r}",
        )
    if expected is not None and head != expected:
        return CheckResult(
            "database.revision",
            False,
            f"database at expected {expected!r} but image head is {head!r}",
        )
    return CheckResult("database.revision", True, f"{current} (head {head})")


def check_runtime_opens(config: DeploymentConfig) -> CheckResult:
    """Open the real store through the public API and perform one repository read.

    Safe to run against production data: the revision is checked first, so
    ``AER(...)`` finds nothing to migrate, and the only query is a ``COUNT``.
    """
    try:
        with AER(config.db_path.parent, db_filename=config.db_path.name) as runtime:
            runs = runtime.runs.count()
            experiences = runtime.experiences.count()
    except AERError as exc:
        return CheckResult("runtime.open", False, f"{type(exc).__name__}: {exc}")

    return CheckResult(
        "runtime.open",
        True,
        f"runs={runs} experiences={experiences} data_dir={runtime.data_dir.as_posix()}",
    )


def check_temporary_roundtrip() -> CheckResult:
    """Prove the write path in a throwaway database, never in production data."""
    try:
        with (
            tempfile.TemporaryDirectory(prefix="aer-smoke-") as workspace,
            AER(workspace) as runtime,
        ):
            run = runtime.start_run(task="smoke test", task_type="smoke")
            run.success()
            recorded = runtime.get_run(run.run_id)
            revision = runtime.database.schema_revision()
            tables = runtime.database.table_names()
    except AERError as exc:
        return CheckResult("runtime.temp_roundtrip", False, f"{type(exc).__name__}: {exc}")

    if recorded is None:
        return CheckResult("runtime.temp_roundtrip", False, "run was not persisted")
    if revision != head_revision():
        return CheckResult(
            "runtime.temp_roundtrip", False, f"temp database at {revision!r}, expected head"
        )
    return CheckResult(
        "runtime.temp_roundtrip",
        True,
        f"run {run.run_id} persisted, revision {revision}, {len(tables)} tables",
    )


def run_checks(
    config: DeploymentConfig,
    *,
    expected_revision: str | None,
    temp_db_check: bool,
) -> list[CheckResult]:
    """Every check, in the order that makes each one's guard valid."""
    # Revision *before* opening: AER migrates on construction, so a stale revision
    # must be reported, not silently fixed by the smoke test.
    revision = check_schema_revision(config.db_path, expected=expected_revision)
    results = [
        CheckResult("runtime.version", True, f"aer {__version__}, python {sys.version.split()[0]}"),
        CheckResult(
            "config.resolve",
            True,
            json.dumps(config.describe(), sort_keys=True),
        ),
        check_directories_writable(
            {
                "data": config.data_dir,
                "artifact": config.artifact_dir,
                "knowledge": config.knowledge_dir,
                "backup": config.backup_dir,
            }
        ),
        check_database_integrity(config.db_path),
        revision,
        _check_runtime_open(config, revision),
    ]
    if temp_db_check:
        results.append(check_temporary_roundtrip())
    return results


#: Shown when the runtime open is not attempted. Removing the last chance to see a
#: database's revision before it changes is the failure this check now avoids.
REVISION_PRECEDENCE_DETAIL = (
    "not attempted: the database is not at the revision this image ships, and "
    "AER(...) applies pending migrations on construction -- opening it would rewrite "
    "the schema of the very database this check promises not to modify"
)


def _check_runtime_open(config: DeploymentConfig, revision: CheckResult) -> CheckResult:
    """Open the store through the public API, but only when that cannot migrate it.

    ``AER(...)`` calls ``upgrade_to_head`` in its constructor, so "open the store"
    and "read the store" are the same thing only while the database is already at
    head. On anything older -- which is exactly what a restored older backup is --
    the open *is* a migration.

    A cross-revision recovery drill caught the consequence: the smoke test reported
    ``database.revision`` as failed and then upgraded the database from 0003 to 0004
    anyway, so the operator could no longer see what they had restored. The check is
    skipped, and reported as **unmet rather than passed** -- nothing was proven
    about this database.
    """
    if not revision.ok:
        return CheckResult("runtime.open", False, REVISION_PRECEDENCE_DETAIL)
    return check_runtime_opens(config)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="smoke_test.py",
        description="Read-only production smoke test for an AER deployment.",
    )
    parser.add_argument("--data-dir", type=Path, help="override AER_DATA_DIR")
    parser.add_argument("--db-path", type=Path, help="override AER_DB_PATH")
    parser.add_argument(
        "--expect-revision",
        help="the Alembic revision this release expects the database to be at",
    )
    parser.add_argument(
        "--temp-db-check",
        action="store_true",
        help="also prove the write path in a throwaway database",
    )
    parser.add_argument("--json", action="store_true", help="print results as JSON on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)

    def log(message: str) -> None:
        print(f"[smoke] {message}", file=sys.stderr, flush=True)

    config = load_deployment_config()
    if args.data_dir is not None or args.db_path is not None:
        # Re-resolve through the same loader rather than editing the dataclass, so
        # the production path rules still apply to overrides. `os` is imported at
        # module level; the duplicate that used to live here shadowed it.
        overrides = dict(os.environ)
        if args.data_dir is not None:
            overrides["AER_DATA_DIR"] = str(args.data_dir)
        if args.db_path is not None:
            overrides["AER_DB_PATH"] = str(args.db_path)
        config = load_deployment_config(overrides)

    log(f"aer {__version__} | environment={config.environment}")
    try:
        results = run_checks(
            config,
            expected_revision=args.expect_revision,
            temp_db_check=args.temp_db_check,
        )
    except AERError as exc:
        print(f"[smoke] FAILED to run checks: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    failures = [result for result in results if not result.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "aer_version": __version__,
                    "environment": config.environment,
                    "passed": not failures,
                    "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for result in results:
            print(f"[{'ok  ' if result.ok else 'FAIL'}] {result.name}: {result.detail}")

    if failures:
        log(f"{len(failures)} of {len(results)} checks FAILED: {[f.name for f in failures]}")
        return 1
    log(f"all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
