"""The knowledge-index seam: what an index is, not which one we use.

AER keeps exactly one source of truth -- SQLite -- and projects *from* it into a
graph index that exists to be searched. This module is the boundary between the
two, and it exists for one reason worth stating plainly:

    the experience and runtime layers must not know that NeuG exists.

That is not "so we can swap databases later". It is so that
:class:`~aer.knowledge.projector.KnowledgeProjector` and
:class:`~aer.knowledge.retriever.ExperienceRetriever` contain business rules --
policy, ranking, trust -- and no Cypher. The graph query language, the parameter
conventions, the result shape and the extension-loading dance all stay on the far
side of :class:`KnowledgeIndex`.

The records crossing this boundary are intentionally *dumb*: strings and booleans
copied from the store, with enums left as strings. Decoding them into
:class:`~aer.runtime.enums.ExperienceKind` and friends is the retriever's job,
because "what does this status mean" is a knowledge-plane question and the index
is not entitled to an opinion about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "IndexFilters",
    "IndexMatch",
    "IndexQuery",
    "IndexedExperience",
    "IndexedRunRef",
    "KnowledgeIndex",
]


@dataclass(frozen=True, slots=True)
class IndexedExperience:
    """One experience as the index stores it: flat, stringly, no semantics.

    ``root_cause`` and ``solution`` are ``str`` rather than ``str | None`` on
    purpose. NeuG persistent properties currently reject ``NULL`` and reject
    assigning one, so the projection maps ``None`` to the empty string at this
    boundary. The distinction between "no known cause" and "a cause that happens
    to be blank" is preserved upstream in SQLite, which is the source of truth;
    the index only has to answer relevance questions, and both spellings are
    equally irrelevant to a term match.
    """

    id: str
    kind: str
    status: str
    domain: str
    title: str
    problem: str
    root_cause: str
    solution: str
    failed_attempts_text: str
    avoid_text: str
    outcome_verified: bool
    generalizable: bool
    created_at: str
    updated_at: str
    run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexedRunRef:
    """The lightweight run pointer a graph edge can terminate at.

    Not a copy of the run. agent.md #25 and section 14 of the round-6 brief both
    insist that the full run stays in SQLite; this carries just enough context for
    a human reading a retrieval result to know *which* execution a piece of advice
    came from and whether that execution ended well.
    """

    id: str
    status: str
    agent_name: str
    agent_version: str


@dataclass(frozen=True, slots=True)
class IndexFilters:
    """Conditions the index must apply **before** choosing its top-K.

    Pushing these down is the whole reason the index is a graph database instead
    of a text blob: filtering 1000 candidates in Python after the fact would make
    the ranking wrong (the top 5 overall are not the top 5 among the eligible set)
    as well as slow.

    ``None`` means "no constraint", which is different from an empty tuple --
    an empty tuple matches nothing and is therefore a caller error that produces
    an empty result rather than a wider one.

    Deprecation is **not** a separate flag. A deprecated experience is simply one
    whose status is ``DEPRECATED``, so the status set is the single authority on
    what may be returned; a second switch that could disagree with the set is a
    bug waiting for a caller to find it.
    """

    kinds: tuple[str, ...] | None = None
    statuses: tuple[str, ...] | None = None
    outcome_verified: bool | None = None


@dataclass(frozen=True, slots=True)
class IndexQuery:
    """A text search plus the filters and the size of the answer set."""

    text: str
    filters: IndexFilters
    limit: int
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class IndexMatch:
    """One search hit, exactly as the index stored it.

    ``bm25_score`` is carried through raw and untranslated. NeuG's BM25 is
    *lower-is-better* (the official FTS page says so, and the probe re-checked it),
    but that convention is a property of the engine, not of retrieval. Converting
    it into something monotone-increasing is
    :mod:`aer.knowledge.ranking`'s job, in one place, with a test pinning the
    direction -- so that if the engine ever flips, exactly one function changes.
    """

    experience_id: str
    kind: str
    status: str
    domain: str
    title: str
    problem: str
    root_cause: str
    solution: str
    failed_attempts_text: str
    avoid_text: str
    outcome_verified: bool
    generalizable: bool
    created_at: str
    updated_at: str
    bm25_score: float


@runtime_checkable
class KnowledgeIndex(Protocol):
    """The operations the knowledge plane needs from a graph index.

    Deliberately small and deliberately synchronous. There is no ``async``
    variant, no connection pool, no retry policy: AER is embedded, one process,
    one caller, and a retry loop around a local file open would only hide the
    failures this protocol exists to surface.
    """

    def ensure_schema(self) -> None:
        """Create the projection schema if it is missing. Repeatable."""
        ...

    def projection_version(self) -> int | None:
        """The projection schema version recorded beside the data, or ``None``."""
        ...

    def upsert_experiences(
        self,
        experiences: tuple[IndexedExperience, ...],
        run_refs: tuple[IndexedRunRef, ...],
    ) -> None:
        """Write experiences (and the run references they derive from).

        Must be idempotent for the same input, must replace the previous content
        of an experience rather than accumulate, and must keep every source
        relation. Whether that is done by deleting first, by MERGE, or by a diff is
        an implementation detail the caller must not depend on.
        """
        ...

    def delete_experiences(self, experience_ids: tuple[str, ...]) -> None:
        """Remove projections for experiences that no longer exist in SQLite."""
        ...

    def search(self, query: IndexQuery) -> list[IndexMatch]:
        """Ranked matches, best first, with the filters applied inside the index."""
        ...

    def source_counts(self, experience_ids: tuple[str, ...]) -> dict[str, int]:
        """How many distinct runs each experience is derived from.

        One batched call rather than one per hit: section 83 of the round-6 brief
        forbids an N+1 pattern between retrieval and the index.
        """
        ...

    def fingerprints(self) -> dict[str, str]:
        """Map of experience id to the ``updated_at`` the index currently holds.

        The drift detector's raw material. SQLite's own ``updated_at`` for the same
        id is compared against this; a mismatch, a missing id or a surplus id all
        mean the projection is stale and a rebuild is due.
        """
        ...

    def count_experiences(self, *, include_deprecated: bool = True) -> int:
        """How many experience nodes the index holds."""
        ...

    def reset(self) -> None:
        """Drop the projection so it can be rebuilt from scratch."""
        ...

    def close(self) -> None:
        """Release the underlying database handle. Idempotent."""
        ...
