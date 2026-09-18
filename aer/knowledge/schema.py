"""Projection schema V1: the shape of the graph index, in one place.

Three node labels and two edge types, because that is what the retrieval policy
actually needs and no more:

* ``Experience``  -- the searchable knowledge;
* ``RunRef``      -- a thin pointer back to a run, so provenance survives into the
  index without copying the run;
* ``Domain``      -- a real node rather than a string property, so "everything
  about WordPress" is one graph hop instead of a scan.

Adding ``Tool``, ``ErrorPattern``, ``Workflow``, ``Skill`` and friends was
explicitly deferred (section 12 of the round-6 brief). An ontology invented before
the queries that need it produces joins nobody traverses and a schema that has to
be rebuilt to remove them.

Two things about this schema are not obvious and are load-bearing:

**Strings need an explicit width.** In NeuG ``STRING`` is shorthand for
``VARCHAR(256)``. An experience's ``problem`` or ``solution`` routinely runs to
several hundred characters of Chinese, and a silently truncated knowledge store is
worse than one that refuses the write -- it answers questions with half a sentence.
Long text therefore declares ``VARCHAR(65535)``.

**The projection is versioned by a number, not migrated.** The database is
rebuildable from SQLite in seconds, so a schema change means bump the version and
rebuild; there is no Alembic for the graph side (section 77). The version lives in
a sidecar file so that ``knowledge status`` can report it without opening the
database -- and so that a knowledge directory copied without its sidecar is
treated as unknown rather than assumed current.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = [
    "EXPERIENCE_INDEXED_PROPERTIES",
    "FTS_INDEX_NAME",
    "FTS_PROPERTY_WEIGHTS",
    "PROJECTION_METADATA_SUFFIX",
    "PROJECTION_SCHEMA_VERSION",
    "SCHEMA_STATEMENTS",
    "TOKENIZER_ENV_VAR",
    "fts_index_statement",
    "projection_metadata_path",
]

#: Bumped whenever :data:`SCHEMA_STATEMENTS` or :data:`EXPERIENCE_INDEXED_PROPERTIES`
#: changes shape. A mismatch is not repaired -- it is rebuilt.
PROJECTION_SCHEMA_VERSION = 1

#: Name of the sidecar holding projection metadata, relative to the knowledge dir.
PROJECTION_METADATA_SUFFIX = ".projection.json"

#: Environment variable naming an optional jieba user dictionary.
TOKENIZER_ENV_VAR = "AER_NEUG_JIEBA_DICT"

FTS_INDEX_NAME = "experience_fts"

#: Properties covered by the full-text index, in the order the weights expect.
EXPERIENCE_INDEXED_PROPERTIES: tuple[str, ...] = (
    "title",
    "problem",
    "root_cause",
    "solution",
    "failed_attempts_text",
    "avoid_text",
)

#: Relative importance per indexed property, positionally matched to
#: :data:`EXPERIENCE_INDEXED_PROPERTIES`.
#:
#: ``title`` outranks everything because in this store the title *is* the problem
#: statement -- ``Experience.title`` and ``Experience.problem`` are written by the
#: distiller as the short and the long form of the same claim. ``avoid`` and
#: ``failed_attempts_text`` are last because they describe what went wrong, which
#: is useful corroboration but a poor thing to rank on.
FTS_PROPERTY_WEIGHTS: tuple[float, ...] = (4.0, 3.0, 2.0, 2.0, 1.0, 1.0)

#: DDL for projection schema V1. Every statement is idempotent.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE NODE TABLE IF NOT EXISTS Experience (
        id VARCHAR(128),
        kind VARCHAR(32),
        status VARCHAR(32),
        domain VARCHAR(128),
        title VARCHAR(1024),
        problem VARCHAR(65535),
        root_cause VARCHAR(65535),
        solution VARCHAR(65535),
        failed_attempts_text VARCHAR(65535),
        avoid_text VARCHAR(65535),
        outcome_verified BOOL,
        generalizable BOOL,
        created_at VARCHAR(40),
        updated_at VARCHAR(40),
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RunRef (
        id VARCHAR(128),
        status VARCHAR(32),
        agent_name VARCHAR(256),
        agent_version VARCHAR(64),
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Domain (
        name VARCHAR(128),
        PRIMARY KEY (name)
    )
    """,
    "CREATE REL TABLE IF NOT EXISTS DERIVED_FROM (FROM Experience TO RunRef)",
    "CREATE REL TABLE IF NOT EXISTS APPLIES_TO (FROM Experience TO Domain)",
)


def fts_index_statement(environ: Mapping[str, str] | None = None) -> str:
    """The ``CREATE INDEX ... USING FTS`` statement for schema V1.

    The tokenizer is ``jieba`` unconditionally, including on a corpus that happens
    to be entirely English today. The alternative -- guessing per deployment -- is
    worse: a knowledge store is built to outlive the language of its first few
    entries, and a mixed Chinese/English corpus under the default ``unicode61``
    tokenizer degrades quietly rather than failing loudly.

    ``jieba_dict`` is only added when an operator has actually supplied a
    dictionary. The built-in vocabulary is ~110k entries and needs no help to
    start; requiring a custom dictionary in production would make the default
    deployment the unsupported one.
    """
    source = os.environ if environ is None else environ
    options = ["tokenizer = 'jieba'", "jieba_mode = 'mix'"]
    dictionary = (source.get(TOKENIZER_ENV_VAR) or "").strip()
    if dictionary:
        options.append(f"jieba_dict = {_quote(dictionary)}")
    columns = ", ".join(EXPERIENCE_INDEXED_PROPERTIES)
    return (
        f"CREATE INDEX {FTS_INDEX_NAME} ON Experience USING FTS ({columns}) "
        f"WITH ({', '.join(options)})"
    )


def projection_metadata_path(database_path: str) -> str:
    """Sidecar path for ``database_path``.

    A sibling rather than a child, because NeuG decides for itself whether the
    path it is handed becomes a file or a directory and this module must not
    depend on which.
    """
    return database_path + PROJECTION_METADATA_SUFFIX


def _quote(value: str) -> str:
    """Single-quote a Cypher string literal.

    Used only for the jieba dictionary path, which comes from an operator's
    environment variable rather than from data. Escaping is still applied: an
    environment variable is not a licence to concatenate arbitrary text into a
    statement, and a path containing a quote is far more likely than one
    containing an injected clause.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
