"""Compare two SQLite files byte-wise and page-wise.

Answers two questions a drill must not hand-wave:

* *Where* do the bytes differ (so a differing checksum can be explained rather
  than ignored)?
* Are the *logical* contents equal even though the bytes are not?

The SQLite header carries a change counter (offset 24) and a version-valid-for
number (offset 92) that are bumped on write, so two files holding identical data
routinely have different checksums. That is why the drill compares facts, not
hashes.

Usage: python drill_compare.py <file-a> <file-b> [--a-immutable] [--b-immutable]

``--a-immutable`` / ``--b-immutable`` are needed when one side is a WAL-mode
database mounted read-only: a plain ``mode=ro`` connection would then want to
create ``-shm`` and fail.

``--shared-tables`` compares only the tables both files have, ignoring
``alembic_version``: the question a cross-revision drill asks is whether the
migration preserved what already existed, and the migration is *supposed* to add
tables and bump the revision.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compare(
    a: str,
    b: str,
    a_immutable: bool,
    b_immutable: bool,
    *,
    shared_tables_only: bool = False,
) -> dict[str, object]:
    first, second = Path(a), Path(b)
    raw_a, raw_b = first.read_bytes(), second.read_bytes()
    report: dict[str, object] = {
        "a": {"path": first.as_posix(), "bytes": len(raw_a), "sha256": _digest(raw_a)},
        "b": {"path": second.as_posix(), "bytes": len(raw_b), "sha256": _digest(raw_b)},
        "byte_identical": raw_a == raw_b,
    }

    # `strict=False` on purpose: two files of different length are a legitimate
    # input (that difference is itself a finding), so the pairwise comparison is
    # deliberately truncated and the length difference is added back below.
    differing = [i for i, (x, y) in enumerate(zip(raw_a, raw_b, strict=False)) if x != y]
    report["differing_byte_count"] = len(differing) + abs(len(raw_a) - len(raw_b))
    report["first_differing_offset"] = differing[0] if differing else None

    # The header's change counter (4 bytes at offset 24) is expected to differ.
    report["header_change_counter"] = {
        "a": int.from_bytes(raw_a[24:28], "big"),
        "b": int.from_bytes(raw_b[24:28], "big"),
    }
    report["header_first_100_bytes_identical"] = raw_a[:100] == raw_b[:100]

    # Logical equality: schema inventory and content hash of every table.
    logical_a, logical_b = _logical(a, a_immutable), _logical(b, b_immutable)
    contents_a: dict[str, object] = dict(logical_a["contents"])  # type: ignore[arg-type]
    contents_b: dict[str, object] = dict(logical_b["contents"])  # type: ignore[arg-type]
    report["tables_only_in_a"] = sorted(set(contents_a) - set(contents_b))
    report["tables_only_in_b"] = sorted(set(contents_b) - set(contents_a))

    report["shared_tables_only"] = shared_tables_only
    if shared_tables_only:
        # `alembic_version` is excluded deliberately: its whole job is to change
        # when a migration runs, so including it would make this comparison always
        # report a difference and say nothing about the data.
        shared = (set(contents_a) & set(contents_b)) - {"alembic_version"}
        contents_a = {table: contents_a[table] for table in sorted(shared)}
        contents_b = {table: contents_b[table] for table in sorted(shared)}
        logical_a = {"contents": contents_a}
        logical_b = {"contents": contents_b}

    report["logical"] = {"a": logical_a, "b": logical_b}
    report["logical_equal"] = contents_a == contents_b
    return report


def _logical(path: str, immutable: bool) -> dict[str, object]:
    """Schema text plus a per-table content digest, ordered deterministically."""
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?{query}", uri=True)) as conn:
        schema = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [
            row[1] for row in schema if row[0] == "table" and not row[1].startswith("sqlite_")
        ]
        contents: dict[str, object] = {}
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            payload = json.dumps(
                [[_normalise(value) for value in row] for row in rows],
                sort_keys=True,
                default=repr,
            ).encode()
            contents[table] = {"rows": len(rows), "digest": _digest(payload)}
    return {"schema": schema, "contents": contents}


def _normalise(value: object) -> object:
    return {"__bytes_hex__": value.hex()} if isinstance(value, bytes) else value


def main(argv: list[str]) -> int:
    flags = {argument for argument in argv[1:] if argument.startswith("--")}
    paths = [argument for argument in argv[1:] if not argument.startswith("--")]
    if len(paths) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(
        json.dumps(
            compare(
                paths[0],
                paths[1],
                "--a-immutable" in flags,
                "--b-immutable" in flags,
                shared_tables_only="--shared-tables" in flags,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
