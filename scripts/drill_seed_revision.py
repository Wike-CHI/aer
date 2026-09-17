"""Seed a drill database with records the *target revision* can actually hold.

The point of this script is what it refuses to do. ``AER(...)`` applies pending
migrations on construction (``Database.__init__`` calls ``upgrade_to_head``), so
pointing the current runtime at a revision-0003 store would upgrade it to 0004 --
and a cross-revision drill that starts by accidentally upgrading the database tests
nothing at all.

So the rows are written as parameterised SQL. The column lists are declared per
revision, taken from ``migrations/versions/0001`` .. ``0003``; they are *data*, not
a compatibility layer. Before writing anything the script asserts that

* the database really is at the revision it was asked to seed, and
* every declared column really exists, and
* the tables it is about to write are empty.

A wrong declaration therefore fails loudly instead of producing a store that looks
plausible and is subtly not what an older AER would have written.

Usage::

    python drill_seed_revision.py <data-dir> --revision 0003 [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

#: The columns each table has *as of* the given revision. Declared rather than
#: discovered, because the whole purpose is to write what that revision's schema
#: accepts -- and because a mistake here must be visible, not adapted around.
REVISION_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "0002": {
        "runs": (
            "id",
            "task_type",
            "task_description",
            "agent_name",
            "agent_version",
            "model_provider",
            "model_name",
            "status",
            "started_at",
            "ended_at",
            "final_score",
            "metadata_json",
        ),
        "events": (
            "id",
            "run_id",
            "sequence",
            "event_type",
            "input_json",
            "output_json",
            "created_at",
            "duration_ms",
            "metadata_json",
        ),
        "errors": (
            "id",
            "run_id",
            "event_id",
            "error_type",
            "error_message",
            "stack_trace",
            "recoverable",
            "resolved",
            "created_at",
            "metadata_json",
        ),
        "recoveries": (
            "id",
            "run_id",
            "error_id",
            "start_event_id",
            "result_event_id",
            "reason",
            "success",
            "duration_ms",
            "outcome_json",
            "started_at",
            "ended_at",
            "metadata_json",
        ),
    },
    # 0003 adds exactly one table (verifications); nothing else changes, which is
    # why only that entry differs.
    "0003": {},
}
REVISION_COLUMNS["0003"] = {
    **REVISION_COLUMNS["0002"],
    "verifications": (
        "id",
        "run_id",
        "event_id",
        "verifier_type",
        "verifier_name",
        "passed",
        "required",
        "score",
        "message",
        "result_json",
        "created_at",
        "metadata_json",
    ),
}

#: Tables in the order they must be written: foreign keys point backwards.
WRITE_ORDER = ("runs", "events", "errors", "recoveries", "verifications")

#: A fixed clock, so the drill's evidence is reproducible. `ended_at` too: a run
#: that is finished must look finished, or the runtime will report it as RUNNING.
BASE = "2026-09-16 09:00:"
MARKER = "seeded at revision {revision}"


def _at(seconds: int) -> str:
    return f"{BASE}{seconds:02d}.000000"


def _json(payload: dict[str, object]) -> str:
    """The same encoding ``aer.storage.converters`` writes."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Row:
    """One fixture row: a table name and its column values."""

    table: str
    values: dict[str, object]


def fixture(revision: str) -> list[Row]:
    """The records to write, given what ``revision`` can hold.

    Shaped like real history: one run that succeeded and was verified, one run that
    failed and recovered. The recovery links to its error, the error links to its
    ERROR event, and the verification links to its VERIFICATION event -- so the
    drill compares relationships, not just row counts.
    """
    marker = MARKER.format(revision=revision)
    rows: list[Row] = [
        Row(
            "runs",
            {
                "id": "run-0003-verified",
                "task_type": "drill",
                "task_description": "cross-revision drill: a verified success",
                "agent_name": "drill-agent",
                "agent_version": "0.5.0",
                "model_provider": "none",
                "model_name": "none",
                "status": "SUCCESS",
                "started_at": _at(0),
                "ended_at": _at(6),
                "final_score": None,
                "metadata_json": _json({"marker": marker, "expect": "verified_success"}),
            },
        ),
        Row(
            "runs",
            {
                "id": "run-0003-recovered",
                "task_type": "drill",
                "task_description": "cross-revision drill: a recovered failure",
                "agent_name": "drill-agent",
                "agent_version": "0.5.0",
                "model_provider": "none",
                "model_name": "none",
                "status": "SUCCESS",
                "started_at": _at(10),
                "ended_at": _at(20),
                "final_score": None,
                "metadata_json": _json({"marker": marker, "expect": "recovery"}),
            },
        ),
    ]

    # Run 1: a clean run that was verified afterwards (VERIFICATION is a system
    # event appended after the run reached its terminal status, hence the sequence).
    rows += [
        Row(
            "events",
            {
                "id": 1,
                "run_id": "run-0003-verified",
                "sequence": 1,
                "event_type": "TASK_START",
                "created_at": _at(0),
                "input_json": _json({"task": "drill"}),
                "output_json": None,
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 2,
                "run_id": "run-0003-verified",
                "sequence": 2,
                "event_type": "TOOL_CALL",
                "created_at": _at(1),
                "input_json": _json({"tool": "http_request"}),
                "output_json": None,
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 3,
                "run_id": "run-0003-verified",
                "sequence": 3,
                "event_type": "TOOL_RESULT",
                "created_at": _at(2),
                "input_json": None,
                "output_json": _json({"actual_status": 200}),
                "duration_ms": 120,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 4,
                "run_id": "run-0003-verified",
                "sequence": 4,
                "event_type": "TASK_END",
                "created_at": _at(6),
                "input_json": None,
                "output_json": _json({"status": "SUCCESS"}),
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 5,
                "run_id": "run-0003-verified",
                "sequence": 5,
                "event_type": "VERIFICATION",
                "created_at": _at(6),
                "input_json": None,
                "output_json": _json({"passed": True}),
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        # Run 2: failure, recorded error, successful recovery.
        Row(
            "events",
            {
                "id": 6,
                "run_id": "run-0003-recovered",
                "sequence": 1,
                "event_type": "TASK_START",
                "created_at": _at(10),
                "input_json": _json({"task": "drill"}),
                "output_json": None,
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 7,
                "run_id": "run-0003-recovered",
                "sequence": 2,
                "event_type": "TOOL_CALL",
                "created_at": _at(11),
                "input_json": _json({"tool": "http_request"}),
                "output_json": None,
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 8,
                "run_id": "run-0003-recovered",
                "sequence": 3,
                "event_type": "ERROR",
                "created_at": _at(12),
                "input_json": None,
                "output_json": _json({"recoverable": True}),
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 9,
                "run_id": "run-0003-recovered",
                "sequence": 4,
                "event_type": "RECOVERY_START",
                "created_at": _at(13),
                "input_json": _json({"reason": "retry"}),
                "output_json": None,
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 10,
                "run_id": "run-0003-recovered",
                "sequence": 5,
                "event_type": "RECOVERY_RESULT",
                "created_at": _at(18),
                "input_json": None,
                "output_json": _json({"success": True}),
                "duration_ms": 5000,
                "metadata_json": None,
            },
        ),
        Row(
            "events",
            {
                "id": 11,
                "run_id": "run-0003-recovered",
                "sequence": 6,
                "event_type": "TASK_END",
                "created_at": _at(20),
                "input_json": None,
                "output_json": _json({"status": "SUCCESS"}),
                "duration_ms": None,
                "metadata_json": None,
            },
        ),
        Row(
            "errors",
            {
                "id": "err-0003-1",
                "run_id": "run-0003-recovered",
                "event_id": 8,
                "error_type": "builtins.ConnectionRefusedError",
                "error_message": "connection refused by example.invalid",
                "stack_trace": "Traceback (most recent call last):\\n  ...",
                "recoverable": 1,
                "resolved": 1,
                "created_at": _at(12),
                "metadata_json": _json({"attempt": 1}),
            },
        ),
        Row(
            "recoveries",
            {
                "id": "rec-0003-1",
                "run_id": "run-0003-recovered",
                "error_id": "err-0003-1",
                "start_event_id": 9,
                "result_event_id": 10,
                "reason": "retry once reachable",
                "success": 1,
                "duration_ms": 5000,
                "outcome_json": _json({"attempts": 2}),
                "started_at": _at(13),
                "ended_at": _at(18),
                "metadata_json": _json({"marker": marker}),
            },
        ),
    ]

    if "verifications" in REVISION_COLUMNS[revision]:
        rows.append(
            Row(
                "verifications",
                {
                    "id": "ver-0003-1",
                    "run_id": "run-0003-verified",
                    "event_id": 5,
                    "verifier_type": "DETERMINISTIC",
                    "verifier_name": "http_status",
                    "passed": 1,
                    "required": 1,
                    "score": None,
                    "message": "expected 200, observed 200",
                    "result_json": _json({"actual_status": 200}),
                    "created_at": _at(6),
                    "metadata_json": _json({"marker": marker}),
                },
            )
        )
    return rows


def _revision(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]) for row in rows}


def seed(data_dir: str | Path, revision: str) -> dict[str, object]:
    """Write the fixture for ``revision`` into ``data_dir``/aer.db."""
    if revision not in REVISION_COLUMNS:
        raise ValueError(f"no fixture declared for revision {revision!r}")
    declared = REVISION_COLUMNS[revision]

    database = Path(data_dir) / "aer.db"
    if not database.is_file():
        raise ValueError(f"no database at {database.as_posix()}")

    with closing(sqlite3.connect(database)) as connection:
        actual_revision = _revision(connection)
        if actual_revision != revision:
            # This is the guard that makes the script revision-aware: seeding a
            # revision-0004 store with revision-0003 rows would produce a database
            # that is neither, and the drill would be measuring that instead.
            raise ValueError(
                f"refusing to seed: {database.as_posix()} is at revision "
                f"{actual_revision!r}, not {revision!r}"
            )

        for table in WRITE_ORDER:
            if table not in declared:
                continue
            present = _columns(connection, table)
            missing = sorted(set(declared[table]) - present)
            if missing:
                raise ValueError(f"{table} at {revision} lacks the declared columns: {missing}")
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count:
                raise ValueError(f"{table} already holds {count} rows; refusing to seed twice")

        rows = fixture(revision)
        for row in rows:
            names = declared[row.table]
            unknown = sorted(set(row.values) - set(names))
            if unknown:
                raise ValueError(f"{row.table}: columns outside the declaration: {unknown}")
            placeholders = ", ".join("?" for _ in names)
            columns = ", ".join(f'"{name}"' for name in names)
            connection.execute(
                f'INSERT INTO "{row.table}" ({columns}) VALUES ({placeholders})',
                [row.values[name] for name in names],
            )
        connection.commit()

        return {
            "revision": actual_revision,
            "seeded_tables": sorted({row.table for row in rows}),
            "row_counts": {
                table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in WRITE_ORDER
                if table in declared
            },
            "ids": {
                "runs": [row.values["id"] for row in rows if row.table == "runs"],
                "events": sorted(row.values["id"] for row in rows if row.table == "events"),
                "errors": [row.values["id"] for row in rows if row.table == "errors"],
                "recoveries": [row.values["id"] for row in rows if row.table == "recoveries"],
                "verifications": [row.values["id"] for row in rows if row.table == "verifications"],
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drill_seed_revision.py", description=__doc__)
    parser.add_argument("data_dir", type=Path, help="directory holding aer.db")
    parser.add_argument("--revision", required=True, help="the revision the store is at")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    try:
        summary = seed(args.data_dir, args.revision)
    except (ValueError, sqlite3.Error) as exc:
        print(f"[drill-seed] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"[drill-seed] seeded revision {summary['revision']}: {summary['row_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
