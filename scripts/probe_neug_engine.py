"""Assert the NeuG engine behaviour this project is built on.

Run this **before** changing the pinned ``neug`` version, and after. AER's knowledge
layer is written against observed engine behaviour rather than against its
documentation, and several of those observations contradict the docs:

    docs say                         engine does
    -------------------------------  --------------------------------------------
    "execute() takes multiple        rejects them: "We do not support preparing
     statements separated by ;"       multiple statements in one query"
    "smaller BM25 is more relevant"  also *negative* -- and `1/(1+bm25)` would
                                     invert the ranking or collapse it to a constant
    "LOAD fts"                       fails until INSTALL downloads 8 MB from OSS
    (nothing about it)               a full-text query is FTS5 *syntax*: `wp-json`
                                     raises "no such column: json"

Each check states what AER depends on and fails loudly if the engine stops doing it.
It is not a general test suite -- ``tests/knowledge/test_neug_index.py`` is that --
it is the list of assumptions that would silently produce wrong answers rather than
errors, which is why they are worth a dedicated run.

    python scripts/probe_neug_engine.py [--database DIR]

Exits ``0`` when every expectation holds, ``1`` otherwise. Skips (exit ``0``) when
the engine is not importable, so it can sit in a pipeline that only sometimes has it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from aer.knowledge.schema import fts_index_statement

DDL = (
    "CREATE NODE TABLE IF NOT EXISTS Experience ("
    " id VARCHAR(128), kind VARCHAR(32), status VARCHAR(32), domain VARCHAR(128),"
    " title VARCHAR(1024), problem VARCHAR(65535), root_cause VARCHAR(65535),"
    " solution VARCHAR(65535), failed_attempts_text VARCHAR(65535),"
    " avoid_text VARCHAR(65535), outcome_verified BOOL, generalizable BOOL,"
    " created_at VARCHAR(40), updated_at VARCHAR(40), PRIMARY KEY (id))",
    "CREATE NODE TABLE IF NOT EXISTS RunRef ("
    " id VARCHAR(128), status VARCHAR(32), agent_name VARCHAR(256),"
    " agent_version VARCHAR(64), PRIMARY KEY (id))",
    "CREATE NODE TABLE IF NOT EXISTS Domain (name VARCHAR(128), PRIMARY KEY (name))",
    "CREATE REL TABLE IF NOT EXISTS DERIVED_FROM (FROM Experience TO RunRef)",
    "CREATE REL TABLE IF NOT EXISTS APPLIES_TO (FROM Experience TO Domain)",
)
CREATE = (
    "CREATE (:Experience {id: $id, kind: $kind, status: $status, domain: $domain,"
    " title: $title, problem: $problem, root_cause: $root_cause, solution: $solution,"
    " failed_attempts_text: $failed, avoid_text: $avoid, outcome_verified: $verified,"
    " generalizable: true, created_at: $stamp, updated_at: $stamp})"
)


def count_experiences(connection, where: str = "", **parameters: object) -> int:  # type: ignore[no-untyped-def]
    """``count(e)``, optionally filtered -- the shape most checks below need."""
    statement = f"MATCH (e:Experience) {where} RETURN count(e)".replace("  ", " ")
    return int(next(iter(connection.execute(statement, parameters=parameters)))[0])


def row(**overrides: object) -> dict[str, object]:
    """Parameters for :data:`CREATE`, with any field overridable."""
    values: dict[str, object] = {
        "id": "exp-1",
        "kind": "RECOVERY",
        "status": "VERIFIED",
        "domain": "wordpress",
        "title": "WordPress REST API 403",
        "problem": "WordPress REST API 403 应用密码无效",
        "root_cause": "",
        "solution": "改用有 edit_posts 权限的应用密码",
        "failed": "",
        "avoid": "不要盲目重试",
        "verified": True,
        "stamp": "2026-09-01T00:00:00+00:00",
    }
    values.update(overrides)
    return values


class Results:
    """Collects outcomes so one broken expectation does not hide the others."""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, expectation: str, holds: bool, detail: str = "") -> None:
        mark = "ok  " if holds else "FAIL"
        suffix = f"  -- {detail}" if detail else ""
        print(f"  [{mark}] {expectation}{suffix}", flush=True)
        if holds:
            self.passed += 1
        else:
            self.failures.append(expectation)

    def raises(self, expectation: str, action, *, expect: bool = True) -> None:
        """Assert that ``action`` raises (or does not), for behaviour we rely on."""
        try:
            action()
        except Exception as exc:
            self.check(
                expectation,
                expect,
                f"raised {type(exc).__name__}: {str(exc)[:90]}",
            )
        else:
            self.check(expectation, not expect, "no exception raised")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=None, help="directory to use (default: a temp dir)")
    args = parser.parse_args(argv)

    try:
        import neug
    except ImportError as exc:
        print(f"neug is not importable, nothing to check: {exc}")
        return 0

    results = Results()
    root = args.database or tempfile.mkdtemp(prefix="aer-neug-probe-")
    created_here = args.database is None
    database_path = f"{root}/aer-knowledge"
    os.makedirs(os.path.dirname(database_path) or ".", exist_ok=True)

    print(f"neug {getattr(neug, '__version__', '?')} -- checking the assumptions AER rests on\n")
    try:
        _run(neug, database_path, results, raw_url=os.environ.get("AER_PROBE_RAW", ""))
    finally:
        if created_here:
            shutil.rmtree(root, ignore_errors=True)

    print(f"\n{results.passed} expectations held, {len(results.failures)} changed")
    if results.failures:
        print("\nThe engine no longer behaves the way the knowledge layer assumes:")
        for failure in results.failures:
            print(f"  - {failure}")
        print(
            "\nDo not widen the pin without reading docs/DECISIONS.md D-055: these are\n"
            "the assumptions that would produce wrong answers rather than errors."
        )
        return 1
    return 0


def _run(neug, database_path: str, results: Results, *, raw_url: str) -> None:  # type: ignore[no-untyped-def]
    database = neug.Database(database_path, mode="read-write")
    connection = database.connect()

    print("## extension loading")
    results.raises(
        "LOAD fts succeeds (the image bakes the extension in)",
        lambda: connection.execute("LOAD fts"),
        expect=False,
    )
    results.check("the database path becomes a directory", Path(database_path).is_dir())

    print("\n## schema")
    # The documented schema-introspection procedures are how `ensure_schema` would
    # most naturally ask "does the schema exist". They are not implemented, which is
    # why it asks a different question instead (`_full_text_ready`). Recording the
    # absence here is the point: if a future version implements them, this is where
    # the simpler design becomes available again.
    results.raises(
        "SHOW_NODE_TABLES() does not exist despite being documented",
        lambda: connection.execute("CALL SHOW_NODE_TABLES() RETURN *"),
    )
    for statement in DDL:
        connection.execute(statement)
    for statement in DDL:
        connection.execute(statement)
    results.check("schema DDL is repeatable", True)

    print("\n## the write path")
    connection.execute(CREATE, parameters=row())
    results.check("CREATE accepts $parameters", True)

    long_text = "海" * 900
    connection.execute(CREATE, parameters=row(id="exp-long", title="long", problem=long_text))
    back = list(connection.execute("MATCH (e:Experience {id: 'exp-long'}) RETURN e.problem"))
    results.check(
        "VARCHAR(65535) holds 900 characters (plain STRING would cap at 256)",
        back and back[0][0] == long_text,
        f"in={len(long_text)} out={len(back[0][0]) if back else 'no row'}",
    )

    multiline = "第一个\n第二个"
    connection.execute(CREATE, parameters=row(id="exp-ml", failed=multiline))
    back = list(
        connection.execute("MATCH (e:Experience {id: 'exp-ml'}) RETURN e.failed_attempts_text")
    )
    results.check("newline-joined list text round-trips", back and back[0][0] == multiline)

    results.raises(
        "a duplicate primary key raises (so domain de-duplication must be explicit)",
        lambda: connection.execute("CREATE (:Domain {name: 'wordpress'})"),
    )

    connection.begin_transaction()
    connection.execute(CREATE, parameters=row(id="exp-tx"))
    connection.commit()
    results.check(
        "begin/commit persists",
        bool(list(connection.execute("MATCH (e:Experience {id: 'exp-tx'}) RETURN e.id"))),
    )

    connection.begin_transaction()
    connection.execute(CREATE, parameters=row(id="exp-rb"))
    connection.rollback()
    results.check(
        "rollback discards the write",
        not list(connection.execute("MATCH (e:Experience {id: 'exp-rb'}) RETURN e.id")),
    )

    print("\n## the upsert primitive")
    connection.begin_transaction()
    connection.execute(
        "MATCH (e:Experience {id: $id}) DETACH DELETE e", parameters={"id": "exp-tx"}
    )
    connection.execute(CREATE, parameters=row(id="exp-tx", title="second"))
    connection.commit()
    found = list(connection.execute("MATCH (e:Experience {id: 'exp-tx'}) RETURN count(e), e.title"))
    results.check(
        "DETACH DELETE + CREATE in one transaction leaves one node with the new content",
        found and found[0][0] == 1 and found[0][1] == "second",
        str(found),
    )

    connection.execute(
        "CREATE (:RunRef {id: 'run-1', status: 'SUCCESS', agent_name: 'a', agent_version: '1'})"
    )
    for _ in range(2):
        connection.execute(
            "MATCH (e:Experience {id: 'exp-1'}), (r:RunRef {id: 'run-1'})"
            " CREATE (e)-[:DERIVED_FROM]->(r)"
        )
    edge_count = next(
        iter(connection.execute("MATCH (:Experience)-[:DERIVED_FROM]->(:RunRef) RETURN count(*)"))
    )[0]
    results.check(
        "a repeated edge CREATE is NOT de-duplicated (hence delete-then-recreate)",
        edge_count == 2,
        f"edge count after two identical CREATEs: {edge_count}",
    )

    print("\n## the read path")
    results.check(
        "e.id IN $ids works", count_experiences(connection, "WHERE e.id IN $v", v=["exp-1"]) == 1
    )
    results.check(
        "e.status IN $statuses works",
        count_experiences(connection, "WHERE e.status IN $v", v=["VERIFIED"]) >= 2,
    )
    results.check(
        "e.outcome_verified = $bool works",
        count_experiences(connection, "WHERE e.outcome_verified = $v", v=True) >= 2,
    )
    results.raises(
        "a bare LIMIT $n is rejected (every query must ORDER BY explicitly)",
        lambda: connection.execute(
            "MATCH (e:Experience) RETURN e.id LIMIT $n", parameters={"n": 2}
        ),
    )
    # A parameterised LIMIT is **silently ignored**: with three rows in the table, a
    # query asking for two returns three. `search` therefore inlines the bound as a
    # literal *and* truncates, so the index's own contract holds whatever the engine
    # decides to do. Recorded here because it is exactly the kind of silent difference
    # that surfaces later as "why did retrieval return eleven experiences".
    parameterised = list(
        connection.execute(
            "MATCH (e:Experience) RETURN e.id ORDER BY e.updated_at ASC LIMIT $n",
            parameters={"n": 1},
        )
    )
    results.check(
        "ORDER BY + LIMIT $n is accepted",
        bool(parameterised),
        f"{len(parameterised)} rows for a limit of 1",
    )
    results.check(
        "LIMIT $parameter is ignored (so the bound must be enforced by the caller)",
        len(parameterised) > 1,
        f"asked for 1 of {count_experiences(connection)} rows, got {len(parameterised)}",
    )
    literal = list(connection.execute("MATCH (e:Experience) RETURN e.id LIMIT 1"))
    results.check("LIMIT as a literal is honoured", len(literal) == 1, f"{len(literal)} rows")

    print("\n## explicit transactions are the only atomicity unit")
    results.raises(
        "multiple statements in one execute() are rejected",
        lambda: connection.execute(
            "MATCH (e:Experience) RETURN count(e); MATCH (e:Experience) RETURN count(e)"
        ),
    )
    results.raises(
        "UNWIND with a list parameter is rejected",
        lambda: connection.execute(
            "UNWIND $ids AS i MATCH (r:RunRef {id: i}) RETURN count(r)",
            parameters={"ids": ["run-1"]},
        ),
    )

    print("\n## full-text search")
    connection.execute(
        "CREATE INDEX experience_fts ON Experience USING FTS (title, problem, root_cause,"
        " solution, failed_attempts_text, avoid_text)"
        " WITH (tokenizer = 'jieba', jieba_mode = 'mix')"
    )
    results.check("CREATE INDEX ... USING FTS with the jieba tokenizer", True)
    # The FTS documentation shows `[IF NOT EXISTS]` in its CREATE INDEX syntax and
    # the grammar does not implement it. `ensure_schema` therefore establishes
    # existence by asking the catalogue, not by re-running a statement that would
    # be rejected as a syntax error the second time.
    results.raises(
        "CREATE INDEX IF NOT EXISTS is rejected by the parser (the docs list it)",
        lambda: connection.execute(
            "CREATE INDEX IF NOT EXISTS experience_fts ON Experience USING FTS (title)"
            " WITH (tokenizer = 'jieba')"
        ),
    )
    # `ensure_schema` therefore re-creates the index on the repair path and treats
    # this specific failure as success. The message and code are asserted so that a
    # reworded engine error shows up here rather than as a mysterious inability to
    # open an index that is perfectly fine.
    try:
        connection.execute(fts_index_statement())
    except Exception as exc:
        message = str(exc)
        results.check(
            'a repeated CREATE INDEX says "already exists"',
            "already exists" in message.lower(),
            message[-120:],
        )
    else:
        results.check("a repeated CREATE INDEX says already exists", False, "no exception raised")

    def score(query: str) -> list:
        statement = (
            "MATCH (e:Experience) RETURN e.id,"
            " bm25([e.title, e.problem, e.root_cause, e.solution, e.failed_attempts_text,"
            " e.avoid_text], [4.0, 3.0, 2.0, 2.0, 1.0, 1.0], $q) AS score"
            " ORDER BY score ASC LIMIT $limit"
        )
        return [
            (r[0], r[1]) for r in connection.execute(statement, parameters={"q": query, "limit": 5})
        ]

    ranked = score('"WordPress" OR "REST" OR "API" OR "403"')
    results.check("a quoted-OR expression is accepted", bool(ranked))
    results.check(
        "BM25 is negative and lower is a better match",
        bool(ranked) and ranked[0][1] < 0 and ranked[0][0] == "exp-1",
        str([(r[0], round(r[1], 4)) for r in ranked][:3]),
    )

    raw = raw_url or "wp-json 404"
    results.raises(
        f"raw caller text {raw!r} is rejected by the engine (FTS5 syntax)",
        lambda: score(raw),
    )
    results.check(
        "the same text quoted is accepted",
        bool(score('"wp" OR "json" OR "404"')),
    )
    results.check("a query with no matching term returns no rows", score('"完全不相干xyz"') == [])

    print("\n## persistence")
    connection.close()
    database.close()
    started = time.perf_counter()
    database = neug.Database(database_path, mode="read-write")
    connection = database.connect()
    connection.execute("LOAD fts")
    reopened = time.perf_counter() - started
    surviving = count_experiences(connection)
    surviving_edges = next(
        iter(connection.execute("MATCH (:Experience)-[:DERIVED_FROM]->(:RunRef) RETURN count(*)"))
    )[0]
    results.check(
        "the projection, the index and the edges survive a reopen",
        surviving >= 3 and bool(score('"WordPress"')) and surviving_edges == 2,
        f"reopen took {reopened * 1000:.0f} ms",
    )

    print("\n## the cost model that shapes the projector")
    batch = time.perf_counter()
    connection.begin_transaction()
    for i in range(200):
        connection.execute(CREATE, parameters=row(id=f"bulk-{i:04d}"))
    connection.commit()
    in_one = time.perf_counter() - batch

    per_commit = time.perf_counter()
    connection.begin_transaction()
    connection.execute(CREATE, parameters=row(id="single-write"))
    connection.commit()
    one = time.perf_counter() - per_commit
    print(f"  200 writes in one transaction: {in_one * 1000:.0f} ms")
    print(f"  1 write in one transaction:    {one * 1000:.0f} ms")
    results.check(
        "a write-bearing commit costs far more than a statement (batch the commits)",
        one * 5 > in_one / 200 * 5 and one > in_one / 20,
        "if this flips, batching stops being worth the rollback granularity",
    )

    connection.close()
    database.close()


if __name__ == "__main__":
    sys.exit(main())
