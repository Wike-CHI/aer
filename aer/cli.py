"""``aer`` -- the minimal operator CLI (TASKS.md #14, Milestone 11).

The CLI is a thin read-only lens over an AER data directory: it opens the runtime
the same way an embedding application does and prints what is stored. It adds no
behaviour of its own -- every fact on screen comes from the same repositories the
runtime serves, so what ``aer runs`` shows is what a caller of ``list_runs()``
would get.

Commands::

    aer init                       create the data directory and database
    aer runs                       list runs, newest first
    aer run show <id>              one run: verdicts, errors, recoveries, links
    aer experience list            list experiences, newest first
    aer experience show <id>       one experience, with its source runs
    aer experience search "query"  search the knowledge index
    aer stats                      store-wide counts
    aer doctor                     environment and database health checks

Output is plain English text on purpose: this is an operator surface, and half of
it runs on Windows consoles whose codepage mangles UTF-8.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from aer import AER, __version__
from aer.runtime.enums import ExperienceKind, ExperienceStatus, RunStatus
from aer.runtime.runtime import DEFAULT_DB_FILENAME
from aer.storage.migrations import current_revision, head_revision

#: Where the CLI looks for data when neither --data-dir nor the environment says.
DEFAULT_DATA_DIR = "data"

#: Environment variable overriding the default data directory.
ENV_DATA_DIR = "AER_DATA_DIR"

_PROG = "aer"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _data_dir(args: argparse.Namespace) -> Path:
    """Resolve the data directory: flag, then environment, then default."""
    raw = args.data_dir or os.environ.get(ENV_DATA_DIR) or DEFAULT_DATA_DIR
    return Path(raw)


def _short_id(identifier: str) -> str:
    """First block of a UUID -- enough to tell rows apart in a list."""
    return identifier[:8]


def _clip(text: object, width: int) -> str:
    """Render ``text`` as one line no wider than ``width``."""
    rendered = " ".join(str(text).split())
    if len(rendered) <= width:
        return rendered
    return rendered[: width - 1].rstrip() + "…"


def _print_table(headers: tuple[str, ...], rows: Sequence[Sequence[object]]) -> None:
    """A fixed-width table; widths are computed from the content, nothing more."""
    if not rows:
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    layout = "  ".join(f"{{:<{width}}}" for width in widths)
    print(layout.format(*headers))
    print(layout.format(*("-" * width for width in widths)))
    for row in rows:
        print(layout.format(*(str(cell) for cell in row)))


def _fail(message: str) -> int:
    """Print an error and return the failure exit code."""
    print(f"{_PROG}: error: {message}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create the data directory, database and schema."""
    data_dir = _data_dir(args)
    with AER(data_dir) as aer:
        db_path = aer.database.path
        revision = aer.database.schema_revision()
    print(f"AER {__version__} initialized")
    print(f"  data dir : {data_dir.resolve()}")
    print(f"  database : {db_path}")
    print(f"  schema   : {revision or '(no revision recorded)'}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    """List runs, newest first."""
    status = RunStatus(args.status) if args.status else None
    with AER(_data_dir(args)) as aer:
        runs = aer.list_runs(status=status, task_type=args.task_type, limit=args.limit)
        rows = [
            (
                _short_id(run.id),
                run.status.value,
                run.task_type or "-",
                _clip(run.task_description, 48),
                run.started_at.strftime("%Y-%m-%d %H:%M"),
            )
            for run in runs
        ]
    if not rows:
        print("no runs")
        return 0
    _print_table(("ID", "STATUS", "TYPE", "TASK", "STARTED"), rows)
    print(f"({len(rows)} run(s) shown; use '{_PROG} run show <id>' for detail)")
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    """Show one run with its verdicts, errors, recoveries and links."""
    with AER(_data_dir(args)) as aer:
        run = aer.get_run(args.run_id)
        if run is None:
            return _fail(f"no run with id {args.run_id!r}")
        summary = aer.get_verification_summary(run.id)
        events = aer.get_events(run.id)
        errors = aer.get_errors(run.id)
        recoveries = aer.get_recoveries(run.id)
        experiences = aer.get_experiences_for_run(run.id)

        print(f"run        : {run.id}")
        print(f"task       : {_clip(run.task_description, 100)}")
        print(f"type       : {run.task_type or '-'}")
        print(f"status     : {run.status.value}")
        print(f"started    : {run.started_at:%Y-%m-%d %H:%M:%S}")
        if run.ended_at is not None:
            print(f"ended      : {run.ended_at:%Y-%m-%d %H:%M:%S}")
        if run.final_score is not None:
            print(f"score      : {run.final_score}")
        print(
            "verified   : "
            f"{summary.required_passed}/{summary.required_total} required passed"
            f" ({'yes' if aer.verified_success(run.id) else 'no'})"
        )
        if summary.pass_rate is not None:
            print(f"pass rate  : {summary.pass_rate:.2f} over {summary.total} verdict(s)")
        print(f"events     : {len(events)}")
        print(f"errors     : {len(errors)}")
        print(f"recoveries : {len(recoveries)}")
        if experiences:
            print("experiences:")
            for experience in experiences:
                print(
                    f"  {_short_id(experience.id)}  "
                    f"{experience.kind.value}/{experience.status.value}  "
                    f"{_clip(experience.title, 64)}"
                )
    return 0


def cmd_experience_list(args: argparse.Namespace) -> int:
    """List experiences, newest first."""
    kind = ExperienceKind(args.kind) if args.kind else None
    status = ExperienceStatus(args.status) if args.status else None
    with AER(_data_dir(args)) as aer:
        experiences = aer.list_experiences(
            kind=kind,
            status=status,
            domain=args.domain,
            include_deprecated=args.include_deprecated,
            limit=args.limit,
        )
        rows = [
            (
                _short_id(item.id),
                item.kind.value,
                item.status.value,
                item.domain,
                _clip(item.title, 56),
            )
            for item in experiences
        ]
    if not rows:
        print("no experiences")
        return 0
    _print_table(("ID", "KIND", "STATUS", "DOMAIN", "TITLE"), rows)
    print(f"({len(rows)} experience(s) shown; use '{_PROG} experience show <id>' for detail)")
    return 0


def cmd_experience_show(args: argparse.Namespace) -> int:
    """Show one experience with its full claim and source runs."""
    with AER(_data_dir(args)) as aer:
        experience = aer.get_experience(args.experience_id)
        if experience is None:
            return _fail(f"no experience with id {args.experience_id!r}")
        sources = aer.get_experience_sources(experience.id)

        print(f"id            : {experience.id}")
        print(f"kind / status : {experience.kind.value} / {experience.status.value}")
        print(f"domain        : {experience.domain}")
        print(f"title         : {_clip(experience.title, 100)}")
        print(f"problem       : {_clip(experience.problem, 100)}")
        for symptom in experience.symptoms:
            print(f"symptom       : {_clip(symptom, 100)}")
        for attempt in experience.failed_attempts:
            print(f"failed try    : {_clip(attempt, 100)}")
        if experience.root_cause is not None:
            print(f"root cause    : {_clip(experience.root_cause, 100)}")
        if experience.solution is not None:
            print(f"solution      : {_clip(experience.solution, 100)}")
        for step in experience.recommended_workflow:
            print(f"workflow      : {_clip(step, 100)}")
        for entry in experience.avoid:
            print(f"avoid         : {_clip(entry, 100)}")
        print(f"outcome verif.: {experience.outcome_verified}")
        print(
            f"confidence    : {experience.confidence:.2f} (hint "
            f"{experience.metadata.get('confidence_hint', '-')})"
        )
        print(f"created       : {experience.created_at:%Y-%m-%d %H:%M:%S}")
        print(f"source runs   : {len(sources)}")
        for source in sources:
            print(f"  {source.run_id}")
    return 0


def cmd_experience_search(args: argparse.Namespace) -> int:
    """Search the knowledge index for experience relevant to a query."""
    with AER(_data_dir(args)) as aer:
        try:
            result = aer.retrieve(args.query, domain=args.domain, limit=args.limit)
        except Exception as exc:  # index unavailable / empty query: report, do not trace
            return _fail(f"search failed: {exc}")
        for label, hits in (("guidance", result.guidance), ("warnings", result.warnings)):
            if not hits:
                continue
            print(f"{label}:")
            for hit in hits:
                marker = "verified" if hit.outcome_verified else "unverified"
                print(
                    f"  {_short_id(hit.experience_id)}  [{marker}]  "
                    f"{hit.kind.value}  {_clip(hit.title, 60)}"
                )
        if not result.guidance and not result.warnings:
            print("no relevant experience")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Store-wide counts by status and kind."""
    with AER(_data_dir(args)) as aer:
        print(f"AER {__version__}")
        print(f"data dir    : {_data_dir(args).resolve()}")
        print()
        print(f"runs        : {aer.runs.count()}")
        for status in RunStatus:
            count = aer.runs.count(status=status)
            if count:
                print(f"  {status.value:<12} {count}")
        print(f"experiences : {aer.experiences.count(include_deprecated=True)}")
        for kind in ExperienceKind:
            count = aer.experiences.count(kind=kind, include_deprecated=True)
            if count:
                print(f"  {kind.value:<12} {count}")
        knowledge = aer.knowledge_status()
        print(
            "knowledge   : "
            f"{'reachable' if knowledge.reachable else 'UNREACHABLE'} "
            f"({knowledge.index_experiences}/{knowledge.store_experiences} projected)"
        )
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class _Check:
    """One doctor check result, printed ``[ok]`` / ``[FAIL]`` like smoke_test."""

    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def render(self) -> None:
        marker = "ok  " if self.ok else "FAIL"
        print(f"[{marker}] {self.name}: {self.detail}")


def _check_directory(data_dir: Path) -> _Check:
    if not data_dir.exists():
        return _Check("Directory Permission", False, f"{data_dir} does not exist")
    if not os.access(data_dir, os.W_OK):
        return _Check("Directory Permission", False, f"{data_dir} is not writable")
    return _Check("Directory Permission", True, f"{data_dir} is writable")


def _check_sqlite(db_path: Path) -> tuple[_Check, sqlite3.Connection | None]:
    if not db_path.exists():
        return (
            _Check("SQLite", False, f"{db_path} does not exist; run '{_PROG} init' first"),
            None,
        )
    try:
        connection = sqlite3.connect(str(db_path))
        connection.execute("SELECT 1")
    except sqlite3.Error as exc:
        return _Check("SQLite", False, f"cannot open: {exc}"), None
    return _Check("SQLite", True, f"opened {db_path}"), connection


def _check_migration(db_path: Path) -> _Check:
    try:
        current = current_revision(db_path)
    except Exception as exc:  # alembic speaks through many exception types
        return _Check("Migration", False, f"cannot read revision: {exc}")
    expected = head_revision()
    if current != expected:
        return _Check("Migration", False, f"at {current}, head is {expected}")
    return _Check("Migration", True, f"at head {expected}")


def _check_integrity(connection: sqlite3.Connection) -> _Check:
    try:
        (result,) = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        return _Check("Database Integrity", False, str(exc))
    if result != "ok":
        return _Check("Database Integrity", False, result)
    return _Check("Database Integrity", True, "quick_check ok")


def _check_fts5() -> _Check:
    """Whether this SQLite build can serve full-text search at all."""
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        probe.close()
    except sqlite3.Error as exc:
        return _Check("FTS5", False, f"not available in this SQLite build: {exc}")
    return _Check("FTS5", True, "available")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment and database health checks; exit 1 when any check fails."""
    data_dir = _data_dir(args)
    db_path = data_dir / DEFAULT_DB_FILENAME

    checks = [_check_directory(data_dir)]
    sqlite_check, connection = _check_sqlite(db_path)
    checks.append(sqlite_check)
    if connection is not None:
        checks.append(_check_migration(db_path))
        checks.append(_check_integrity(connection))
        connection.close()
    else:
        checks.append(_Check("Migration", False, "skipped: no database"))
        checks.append(_Check("Database Integrity", False, "skipped: no database"))
    checks.append(_check_fts5())
    size = db_path.stat().st_size if db_path.exists() else 0
    checks.append(
        _Check(
            "Configuration",
            True,
            f"aer {__version__}, data dir {data_dir.resolve()}, database {size / 1024:.0f} KiB",
        )
    )

    failed = 0
    for check in checks:
        check.render()
        if not check.ok:
            failed += 1
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser; separate from execution so tests can drive it."""
    parser = argparse.ArgumentParser(prog=_PROG, description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"data directory (default: ${ENV_DATA_DIR} or ./{DEFAULT_DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data directory and database")

    runs = sub.add_parser("runs", help="list runs, newest first")
    runs.add_argument("--status", choices=[member.value for member in RunStatus])
    runs.add_argument("--task-type")
    runs.add_argument("--limit", type=int, default=100)

    run_show = sub.add_parser("run", help="inspect one run")
    run_sub = run_show.add_subparsers(dest="run_command", required=True)
    show = run_sub.add_parser("show", help="show one run in full")
    show.add_argument("run_id")

    experience = sub.add_parser("experience", help="inspect and search experiences")
    exp_sub = experience.add_subparsers(dest="experience_command", required=True)
    exp_list = exp_sub.add_parser("list", help="list experiences, newest first")
    exp_list.add_argument("--kind", choices=[member.value for member in ExperienceKind])
    exp_list.add_argument("--status", choices=[member.value for member in ExperienceStatus])
    exp_list.add_argument("--domain")
    exp_list.add_argument("--include-deprecated", action="store_true")
    exp_list.add_argument("--limit", type=int, default=100)
    exp_show = exp_sub.add_parser("show", help="show one experience in full")
    exp_show.add_argument("experience_id")
    exp_search = exp_sub.add_parser("search", help="search the knowledge index")
    exp_search.add_argument("query")
    exp_search.add_argument("--domain")
    exp_search.add_argument("--limit", type=int, default=5)

    sub.add_parser("stats", help="store-wide counts")
    sub.add_parser("doctor", help="environment and database health checks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "experience":
        handler = {
            "list": cmd_experience_list,
            "show": cmd_experience_show,
            "search": cmd_experience_search,
        }[args.experience_command]
    else:
        handler = {
            "init": cmd_init,
            "runs": cmd_runs,
            "run": cmd_run_show,
            "stats": cmd_stats,
            "doctor": cmd_doctor,
        }[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
