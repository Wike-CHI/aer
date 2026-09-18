"""The only module in AER that knows NeuG exists.

Everything engine-specific lives here: the package import, the Cypher, the
extension load, the parameter names, the fact that ``bm25()`` points backwards,
and the fact that a full-text query is a *syntax* rather than text. Callers see
:class:`~aer.knowledge.base.KnowledgeIndex` and nothing else.

Facts about neug 0.2.0 this file is built on, every one of them measured on the
deployment target rather than read off a page (see ``docs/DECISIONS.md`` D-054 for
the probe transcripts):

* ``Database(path)`` creates a **directory**, and holds an exclusive lock when
  opened read-write.
* ``LOAD fts`` fails until ``INSTALL fts`` has fetched an 8 MB extension from
  Alibaba OSS, so the production image installs it at build time. ``LOAD`` must be
  reissued **per connection**.
* ``execute()`` rejects multiple semicolon-separated statements, so nothing here
  relies on a statement batch being atomic. ``begin_transaction`` / ``commit`` /
  ``rollback`` do work, and are what makes the upsert atomic.
* There is no ``MERGE`` and no implicit de-duplication of relationships: creating
  the same edge twice produces two edges. The upsert therefore deletes the node
  (``DETACH DELETE`` takes its edges) and recreates it, inside one transaction.
* ``UNWIND`` does not accept a list parameter or a list of maps, so batching is
  done by looping inside a transaction. The expensive part is the transaction
  boundary, not the statement, which is why the projector batches commits.
* ``LIMIT $n`` without ``ORDER BY`` is rejected ("must be literal expressions"),
  so every query here orders explicitly.
* ``STRING`` means ``VARCHAR(256)``; the schema declares widths for that reason.
* A full-text query is passed to an FTS5 engine as **syntax**. ``wp-json 404``
  raises ``no such column: json``. Caller text is therefore turned into quoted
  terms by :mod:`aer.knowledge.query` and never forwarded raw.

The import is deferred to first use on purpose. ``neug`` ships no Windows wheel at
all, so a developer checkout on Windows must still be able to import AER, run every
non-graph test and get an actionable error if it actually asks for retrieval.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aer.exceptions import (
    KnowledgeIndexUnavailable,
    KnowledgeSchemaError,
    ProjectionError,
)
from aer.knowledge.base import (
    IndexedExperience,
    IndexedRunRef,
    IndexFilters,
    IndexMatch,
    IndexQuery,
)
from aer.knowledge.query import fts_expression
from aer.knowledge.schema import (
    EXPERIENCE_INDEXED_PROPERTIES,
    FTS_INDEX_NAME,
    FTS_PROPERTY_WEIGHTS,
    PROJECTION_SCHEMA_VERSION,
    SCHEMA_STATEMENTS,
    fts_index_statement,
    projection_metadata_path,
)
from aer.runtime.enums import ExperienceStatus

__all__ = ["NeuGKnowledgeIndex"]

logger = logging.getLogger(__name__)

_DEPRECATED = ExperienceStatus.DEPRECATED.value

#: Properties every search returns, in the order the decoder expects them.
_MATCH_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "status",
    "domain",
    "title",
    "problem",
    "root_cause",
    "solution",
    "failed_attempts_text",
    "avoid_text",
    "outcome_verified",
    "generalizable",
    "created_at",
    "updated_at",
)

#: The weighted BM25 call, built once from the schema's declaration so the weights
#: and the indexed properties cannot drift apart.
_BM25 = "bm25([{columns}], [{weights}], $query)".format(
    columns=", ".join(f"e.{name}" for name in EXPERIENCE_INDEXED_PROPERTIES),
    weights=", ".join(repr(float(weight)) for weight in FTS_PROPERTY_WEIGHTS),
)


class NeuGKnowledgeIndex:
    """A :class:`~aer.knowledge.base.KnowledgeIndex` backed by an embedded NeuG database."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        mode: str = "read-write",
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._path = str(database_path)
        self._mode = mode
        self._environ = dict(os.environ if environ is None else environ)
        self._database: Any = None
        self._connection: Any = None
        self._fts_loaded = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def path(self) -> str:
        """Filesystem path of the knowledge database (a directory)."""
        return self._path

    @property
    def environ(self) -> Mapping[str, str]:
        """The environment this index was configured from.

        Exposed so a rebuild can build its staging index with exactly the same
        tokenizer settings as the live one -- a staged rebuild that quietly used a
        different jieba dictionary would validate fine and search differently.
        """
        return self._environ

    @property
    def _metadata_path(self) -> str:
        return projection_metadata_path(self._path)

    def _connect(self) -> Any:
        """Open the database and a connection, once per instance.

        The database file is created on demand, along with its parent directory,
        because an embedded index that refuses to exist until someone has made a
        directory for it is a worse failure than one that makes it.
        """
        if self._connection is not None:
            return self._connection
        neug = _import_neug()
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._database = neug.Database(self._path, mode=self._mode)
            self._connection = self._database.connect()
        except Exception as exc:
            raise KnowledgeIndexUnavailable(
                f"Could not open the knowledge database at {self._path!r}: {exc}"
            ) from exc
        self._load_extensions()
        return self._connection

    def _load_extensions(self) -> None:
        """``LOAD fts`` once per connection.

        Not fatal when it fails: an index whose schema exists but whose full-text
        extension is missing can still be counted and rebuilt, and the callers that
        need search get a specific error from :meth:`search` instead of being
        unable to report the state of the index at all.
        """
        if self._fts_loaded:
            return
        try:
            self._connection.execute("LOAD fts")
        except Exception as exc:
            logger.warning("LOAD fts failed on %s: %s", self._path, exc)
            return
        self._fts_loaded = True

    def _execute(self, statement: str, parameters: Mapping[str, Any] | None = None) -> Any:
        connection = self._connect()
        try:
            if parameters:
                return connection.execute(statement, parameters=dict(parameters))
            return connection.execute(statement)
        except RuntimeError as exc:
            # The binding raises RuntimeError for everything the engine refuses,
            # including a genuine engine fault. Translate the class, keep the text.
            raise ProjectionError(f"{exc}") from exc

    def close(self) -> None:
        """Close the connection and the database. Safe to call more than once."""
        connection, database = self._connection, self._database
        self._connection, self._database, self._fts_loaded = None, None, False
        for handle, label in ((connection, "connection"), (database, "database")):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception as exc:
                logger.warning("closing the NeuG %s at %s failed: %s", label, self._path, exc)

    def __enter__(self) -> NeuGKnowledgeIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"NeuGKnowledgeIndex({self._path!r})"

    # -- schema ------------------------------------------------------------

    def projection_version(self) -> int | None:
        """The schema version recorded beside the data, or ``None`` if unknown."""
        try:
            raw = Path(self._metadata_path).read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return None
        version = payload.get("projection_schema_version")
        return version if isinstance(version, int) else None

    def ensure_schema(self) -> None:
        """Create schema V1 if absent; refuse an index this build cannot read.

        Raises:
            KnowledgeSchemaError: the recorded version is not this build's, or it is
                unknown while the database already holds experiences -- which means
                it was written by something whose layout cannot be assumed. The
                repair is a rebuild, and saying so is more useful than adapting.
        """
        recorded = self.projection_version()
        if recorded is not None and recorded != PROJECTION_SCHEMA_VERSION:
            raise KnowledgeSchemaError(
                f"The knowledge index at {self._path!r} records projection schema "
                f"version {recorded}, but this build writes version "
                f"{PROJECTION_SCHEMA_VERSION}. Knowledge is rebuildable from SQLite: "
                "run a rebuild instead of trying to migrate the graph."
            )
        if recorded is None and self._has_experiences():
            raise KnowledgeSchemaError(
                f"The knowledge index at {self._path!r} holds experiences but has no "
                "projection metadata, so the layout it was written with cannot be "
                "assumed. Run a rebuild."
            )

        self._connect()
        for statement in SCHEMA_STATEMENTS:
            self._execute(statement)
        # `IF NOT EXISTS` is documented for CREATE INDEX and is what makes a second
        # open a no-op. The first version probed for the index by running a
        # deliberately failing full-text query instead, which worked but wrote an
        # engine error into the log on every open -- a real error, in the log, on a
        # healthy system, is worse than a slightly less direct check.
        #
        # Settings cannot be changed in place (the engine says so explicitly), so an
        # existing index keeps the tokenizer it was built with. That is consistent
        # with the versioning model: a settings change is a version bump, and a
        # version bump is a rebuild.
        try:
            self._execute(fts_index_statement(self._environ))
        except ProjectionError as exc:
            logger.warning("Creating the FTS index at %s failed: %s", self._path, exc)
        self._write_metadata()

    def _has_experiences(self) -> bool:
        """Whether any experience node exists, tolerating a database with no schema."""
        try:
            rows = list(self._execute("MATCH (e:Experience) RETURN count(e)").__iter__())
        except ProjectionError:
            return False
        return bool(rows) and int(rows[0][0]) > 0

    def _write_metadata(self) -> None:
        payload = {
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "database_path": self._path,
            "fts_index": FTS_INDEX_NAME,
            "indexed_properties": list(EXPERIENCE_INDEXED_PROPERTIES),
            "tokenizer_env": self._environ.get("AER_NEUG_JIEBA_DICT", ""),
        }
        try:
            Path(self._metadata_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise KnowledgeSchemaError(
                f"Could not write the projection metadata to {self._metadata_path!r}: {exc}"
            ) from exc

    # -- projection --------------------------------------------------------

    def upsert_experiences(
        self,
        experiences: tuple[IndexedExperience, ...],
        run_refs: tuple[IndexedRunRef, ...],
    ) -> None:
        """Write experiences and their references in **one** transaction.

        One transaction for the whole batch, deliberately. The probe found a
        transaction boundary costs on the order of a second while a statement costs
        about two milliseconds, so committing per experience would turn a rebuild of
        a thousand experiences from seconds into half an hour. Atomicity is
        preserved where it matters -- a batch either lands or does not -- and the
        batch size is the caller's choice.
        """
        if not experiences and not run_refs:
            return
        connection = self._connect()
        try:
            connection.begin_transaction()
            try:
                self._ensure_domains(connection, {e.domain for e in experiences})
                self._ensure_run_refs(connection, run_refs)
                for experience in experiences:
                    self._write_experience(connection, experience)
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except ProjectionError:
            raise
        except RuntimeError as exc:
            raise ProjectionError(
                f"Projecting {len(experiences)} experiences failed: {exc}"
            ) from exc

    def _ensure_domains(self, connection: Any, names: set[str]) -> None:
        """Create the ``Domain`` nodes a batch refers to, once each."""
        if not names:
            return
        existing = self._existing_ids(connection, "MATCH (d:Domain) RETURN d.name", "name")
        for name in sorted(names - existing):
            self._execute("CREATE (:Domain {name: $name})", {"name": name})

    def _ensure_run_refs(self, connection: Any, run_refs: Sequence[IndexedRunRef]) -> None:
        """Create missing ``RunRef`` nodes and refresh the ones already present.

        A reference node is created once and then refreshed on re-projection: a run
        status can legitimately change after the run finished being observed, and a
        pointer that shows the wrong outcome is worse than no pointer.
        """
        if not run_refs:
            return
        existing = self._existing_ids(connection, "MATCH (r:RunRef) RETURN r.id", "id")
        for ref in run_refs:
            if ref.id in existing:
                self._execute(
                    "MATCH (r:RunRef {id: $id}) SET r.status = $status,"
                    " r.agent_name = $agent_name, r.agent_version = $agent_version",
                    {
                        "id": ref.id,
                        "status": ref.status,
                        "agent_name": ref.agent_name,
                        "agent_version": ref.agent_version,
                    },
                )
            else:
                self._execute(
                    "CREATE (:RunRef {id: $id, status: $status, agent_name: $agent_name,"
                    " agent_version: $agent_version})",
                    {
                        "id": ref.id,
                        "status": ref.status,
                        "agent_name": ref.agent_name,
                        "agent_version": ref.agent_version,
                    },
                )

    def _existing_ids(self, connection: Any, statement: str, column: str) -> set[str]:
        """The ids a projection lookup returns, as a set of strings."""
        try:
            return {str(row[0]) for row in connection.execute(statement)}
        except RuntimeError as exc:
            raise ProjectionError(f"{statement!r} failed: {exc}") from exc

    def _write_experience(self, connection: Any, experience: IndexedExperience) -> None:
        """Replace one experience node and its edges.

        Delete-then-create rather than update-in-place because NeuG has neither
        ``MERGE`` nor edge de-duplication: a second ``CREATE`` of the same
        relationship yields a second relationship, and a projector that accumulated
        edges would report a source count that grows every time it runs. Deleting
        the node with ``DETACH`` removes its edges with it, so the result is a
        function of the input and nothing else -- which is what makes re-projection
        idempotent rather than merely convergent.
        """
        self._execute("MATCH (e:Experience {id: $id}) DETACH DELETE e", {"id": experience.id})
        self._execute(
            "CREATE (:Experience {id: $id, kind: $kind, status: $status,"
            " domain: $domain, title: $title, problem: $problem, root_cause: $root_cause,"
            " solution: $solution, failed_attempts_text: $failed_attempts_text,"
            " avoid_text: $avoid_text, outcome_verified: $outcome_verified,"
            " generalizable: $generalizable, created_at: $created_at,"
            " updated_at: $updated_at})",
            {
                "id": experience.id,
                "kind": experience.kind,
                "status": experience.status,
                "domain": experience.domain,
                "title": experience.title,
                "problem": experience.problem,
                "root_cause": experience.root_cause,
                "solution": experience.solution,
                "failed_attempts_text": experience.failed_attempts_text,
                "avoid_text": experience.avoid_text,
                "outcome_verified": experience.outcome_verified,
                "generalizable": experience.generalizable,
                "created_at": experience.created_at,
                "updated_at": experience.updated_at,
            },
        )
        self._execute(
            "MATCH (e:Experience {id: $eid}), (d:Domain {name: $domain})"
            " CREATE (e)-[:APPLIES_TO]->(d)",
            {"eid": experience.id, "domain": experience.domain},
        )
        for run_id in experience.run_ids:
            self._execute(
                "MATCH (e:Experience {id: $eid}), (r:RunRef {id: $rid})"
                " CREATE (e)-[:DERIVED_FROM]->(r)",
                {"eid": experience.id, "rid": run_id},
            )

    def delete_experiences(self, experience_ids: tuple[str, ...]) -> None:
        """Remove projections for experiences that no longer exist in SQLite."""
        if not experience_ids:
            return
        connection = self._connect()
        try:
            connection.begin_transaction()
            try:
                for experience_id in experience_ids:
                    self._execute(
                        "MATCH (e:Experience {id: $id}) DETACH DELETE e",
                        {"id": experience_id},
                    )
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except ProjectionError:
            raise
        except RuntimeError as exc:
            raise ProjectionError(f"Deleting projections failed: {exc}") from exc

    def reset(self) -> None:
        """Empty the projection, keeping the schema.

        Wipes rows rather than dropping tables: ``DROP TABLE Experience`` also
        removes ``DERIVED_FROM``, ``APPLIES_TO`` and the full-text index with it, so
        a drop-based reset silently leaves an index that can no longer be searched.
        """
        connection = self._connect()
        try:
            connection.begin_transaction()
            try:
                self._execute("MATCH (n) DETACH DELETE n")
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except ProjectionError:
            raise
        except RuntimeError as exc:
            raise ProjectionError(f"Resetting the projection failed: {exc}") from exc

    # -- reads -------------------------------------------------------------

    def search(self, query: IndexQuery) -> list[IndexMatch]:
        """Ranked matches with the filters applied inside the engine.

        Returns an empty list without touching the engine when the query contains no
        searchable words: an empty full-text expression is a syntax error, and
        "the caller sent punctuation" is not a reason to raise.
        """
        expression = fts_expression(query.text)
        if not expression:
            return []
        where, parameters = _where_clause(query.filters)
        parameters["query"] = expression
        parameters["limit"] = max(1, int(query.limit))

        columns = ", ".join(f"e.{name}" for name in _MATCH_COLUMNS)
        projection = f"RETURN {columns}, {_BM25} AS score ORDER BY score ASC LIMIT $limit"
        if query.domain is None:
            match = "MATCH (e:Experience)"
        else:
            # The domain is a graph hop, not a string column: "everything about
            # WordPress" is one edge traversal, which is the first thing this index
            # does that a plain full-text table could not.
            match = "MATCH (e:Experience)-[:APPLIES_TO]->(d:Domain)"
            parameters["domain"] = query.domain
            where = f"d.name = $domain AND ({where})" if where else "d.name = $domain"
        statement = f"{match} WHERE {where} {projection}" if where else f"{match} {projection}"
        connection = self._connect()
        try:
            rows = list(connection.execute(statement, parameters=parameters))
        except RuntimeError as exc:
            raise ProjectionError(f"Knowledge search failed: {exc}") from exc
        return [_to_match(row) for row in rows]

    def source_counts(self, experience_ids: tuple[str, ...]) -> dict[str, int]:
        """Distinct supporting runs per experience, in one batched query.

        Every requested id gets an entry, defaulting to zero. An experience with no
        edges simply does not come back from a relationship match, and returning a
        sparse map would make "no sources recorded" indistinguishable from "you did
        not ask me about that one" -- a distinction the formatter's evidence line
        depends on.
        """
        if not experience_ids:
            return {}
        rows = self._execute(
            "MATCH (e:Experience)-[:DERIVED_FROM]->(r:RunRef) WHERE e.id IN $ids"
            " RETURN e.id, count(r)",
            {"ids": list(experience_ids)},
        )
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {experience_id: counts.get(experience_id, 0) for experience_id in experience_ids}

    def fingerprints(self) -> dict[str, str]:
        """Map of experience id to the ``updated_at`` the index holds.

        The drift detector's input. A full scan on purpose: reading only the ids
        would not notice a record whose content changed without its id changing,
        which is the exact case the fingerprint exists to catch.
        """
        rows = self._execute("MATCH (e:Experience) RETURN e.id, e.updated_at")
        return {str(row[0]): str(row[1]) for row in rows}

    def count_experiences(self, *, include_deprecated: bool = True) -> int:
        """How many experience nodes the index holds."""
        if include_deprecated:
            rows = self._execute("MATCH (e:Experience) RETURN count(e)")
        else:
            statuses = [
                status.value
                for status in ExperienceStatus
                if status is not ExperienceStatus.DEPRECATED
            ]
            rows = self._execute(
                "MATCH (e:Experience) WHERE e.status IN $statuses RETURN count(e)",
                {"statuses": statuses},
            )
        return int(next(iter(rows))[0])


def _where_clause(filters: IndexFilters) -> tuple[str, dict[str, Any]]:
    """Cypher conditions for ``filters``, plus the parameters they use.

    Only the conditions that were actually asked for are emitted, so a filter of
    ``None`` costs nothing and an empty tuple still produces the always-false-ish
    ``e.kind IN $kinds`` with an empty list -- which matches nothing, as intended.
    """
    conditions: list[str] = []
    parameters: dict[str, Any] = {}
    if filters.kinds is not None:
        conditions.append("e.kind IN $kinds")
        parameters["kinds"] = list(filters.kinds)
    if filters.statuses is not None:
        conditions.append("e.status IN $statuses")
        parameters["statuses"] = list(filters.statuses)
    if filters.outcome_verified is not None:
        conditions.append("e.outcome_verified = $outcome_verified")
        parameters["outcome_verified"] = filters.outcome_verified
    return " AND ".join(conditions), parameters


def _to_match(row: Sequence[Any]) -> IndexMatch:
    """Decode one result row: the selected properties, then the score.

    The trailing score column is sliced off before the properties are paired with
    their names, and both steps are guarded. The first version of this function
    checked the length and then paired all fifteen values with fourteen names
    anyway, which ``zip(strict=True)`` turned into a ``ValueError`` on every search
    -- invisible locally, where there is no engine to produce the row.
    """
    values = list(row)
    expected = len(_MATCH_COLUMNS) + 1
    if len(values) != expected:
        raise ProjectionError(
            f"Knowledge search returned {len(values)} columns, expected {expected}. "
            "The engine's row shape changed."
        )
    fields = dict(zip(_MATCH_COLUMNS, values[:-1], strict=True))
    score = values[-1]
    return IndexMatch(
        experience_id=str(fields["id"]),
        kind=str(fields["kind"]),
        status=str(fields["status"]),
        domain=str(fields["domain"]),
        title=str(fields["title"]),
        problem=str(fields["problem"]),
        root_cause=str(fields["root_cause"]),
        solution=str(fields["solution"]),
        failed_attempts_text=str(fields["failed_attempts_text"]),
        avoid_text=str(fields["avoid_text"]),
        outcome_verified=bool(fields["outcome_verified"]),
        generalizable=bool(fields["generalizable"]),
        created_at=str(fields["created_at"]),
        updated_at=str(fields["updated_at"]),
        bm25_score=float(score),
    )


def _import_neug() -> Any:
    """Import the engine, or explain precisely why it cannot be imported.

    Deferred rather than module-level so that everything AER does *except*
    retrieval keeps working on a platform NeuG has no wheel for -- which, today,
    includes every Windows development machine.
    """
    try:
        import neug
    except ImportError as exc:
        raise KnowledgeIndexUnavailable(
            "The NeuG engine is not importable, so the knowledge index cannot be "
            "opened. It is a runtime dependency of the production image; on a "
            "development machine without a NeuG wheel the run trace, verification "
            "and experience layers still work and only retrieval is unavailable. "
            f"Underlying error: {exc}"
        ) from exc
    return neug
