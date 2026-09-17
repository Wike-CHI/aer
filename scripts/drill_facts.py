"""Read-only facts about an AER SQLite database.

Written for disaster-recovery drills, where one of the files being read is the
*production* database. Two properties follow from that and are non-negotiable:

* every connection is opened ``mode=ro``; the tool cannot write even by accident;
* on a read-only mount of a WAL-mode database, ``mode=ro`` alone fails with
  ``unable to open database file`` -- a read-only connection wants to create the
  ``-shm`` file, and cannot on a read-only filesystem. ``immutable=1`` is used in
  that case. It is correct here because AER has no daemon: when no writer holds
  the database, ``-wal`` is checkpointed and absent, so the main file is the whole
  state. The open mode used is recorded in the output, because evidence has to
  say how it was obtained.

Why ``immutable=1`` and not a read-write mount: a read-write mount of a WAL
database *creates* ``-wal`` / ``-shm`` beside it. That mutates the production
directory -- even when the database file is untouched -- and is exactly the trap
the drill is meant to avoid.

The tool is deliberately schema-agnostic: tables come from ``sqlite_master``, so a
drill against an older revision reports what is really there instead of failing on
a table that does not exist yet.

Usage (inside the runtime image, script supplied from the host)::

    python drill_facts.py facts   /path/aer.db [--immutable]
    python drill_facts.py samples /path/aer.db [--immutable]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

#: Tables carrying the substantive facts of a run's history. Sampling one row from
#: each lets a drill compare real field values, not merely row counts.
SAMPLED_TABLES = (
    "runs",
    "events",
    "errors",
    "recoveries",
    "verifications",
    "experiences",
    "experience_sources",
)


def _open_ro(path: Path, immutable: bool) -> sqlite3.Connection:
    """Open ``path`` read-only, by URI so the flag cannot be silently ignored."""
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"file:{path.as_posix()}?{query}", uri=True)


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _as_text(value: object) -> object:
    """Normalise a column value so the JSON round-trip is lossless.

    ``bytes`` would raise in ``json.dumps``; a BLOB that came back as hex can be
    compared against the same column read from another file.
    """
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return {"__repr__": repr(value)}


def facts(path: str, immutable: bool) -> dict[str, object]:
    target = Path(path)
    report: dict[str, object] = {
        "path": target.as_posix(),
        "open_mode": "mode=ro&immutable=1" if immutable else "mode=ro",
        "exists": target.exists(),
    }
    if not target.exists():
        return report

    report["bytes"] = target.stat().st_size
    with closing(_open_ro(target, immutable)) as conn:
        report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        report["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # Physical shape. Recorded because a restored file is *not* expected to be
        # byte-identical to its source -- page ordering and the header's change
        # counter legitimately differ -- so a checksum comparison would report a
        # false alarm. These are the numbers that must match instead.
        report["page_size"] = conn.execute("PRAGMA page_size").fetchone()[0]
        report["page_count"] = conn.execute("PRAGMA page_count").fetchone()[0]
        report["freelist_count"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
        tables = _tables(conn)
        report["table_count"] = len(tables)
        report["tables"] = tables
        report["row_counts"] = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
            if table != "alembic_version"
        }
        report["alembic_revision"] = (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if "alembic_version" in tables
            else None
        )
    return report


def samples(path: str, immutable: bool) -> dict[str, object]:
    """One representative row per substantive table, with every column as text.

    ``ORDER BY rowid`` rather than any timestamp: rowid is the only ordering
    guaranteed to exist and to stay stable across a restore. A drill must prove the
    *same* row survived, so the selector cannot depend on data semantics.
    """
    target = Path(path)
    result: dict[str, object] = {
        "path": target.as_posix(),
        "open_mode": "mode=ro&immutable=1" if immutable else "mode=ro",
    }
    with closing(_open_ro(target, immutable)) as conn:
        available = set(_tables(conn))
        picked: dict[str, object] = {}
        for table in SAMPLED_TABLES:
            if table not in available:
                picked[table] = {"present": False}
                continue
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid LIMIT 1')
            row = cursor.fetchone()
            if row is None:
                picked[table] = {"present": True, "rows": 0}
                continue
            columns = [column[0] for column in cursor.description]
            picked[table] = {
                "present": True,
                "columns": columns,
                "row": {name: _as_text(value) for name, value in zip(columns, row, strict=True)},
            }
        result["samples"] = picked
    return result


def main(argv: list[str]) -> int:
    args = [argument for argument in argv[1:] if argument != "--immutable"]
    immutable = "--immutable" in argv
    if len(args) != 2 or args[0] not in {"facts", "samples"}:
        print(__doc__, file=sys.stderr)
        return 2
    payload = facts(args[1], immutable) if args[0] == "facts" else samples(args[1], immutable)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
