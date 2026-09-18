"""Retrieval: policy, then the index, then ranking -- in that order, visibly.

The pipeline this module implements is the answer to "what is allowed to reach an
agent, and how sure are we":

    query -> policy filters -> index (filters pushed down) -> rank -> split

Three properties are bought by keeping the steps separate rather than folding them
into one ``search()``:

**The policy is a filter on the database, not a check on the results.** A
``GUIDANCE`` query asks the index for confirmed successes and recoveries only. The
alternative -- fetch the best ten, drop the ones that are not confirmed -- returns
the wrong ten the moment a good failure outranks a mediocre success, and the top-K
the user sees would depend on how many rows happened to be dropped.

**Guidance and warnings are ranked separately.** They answer different questions
("what works" vs "what has been tried and did not"), and a single ranked list
would let a popular failure crowd out the one piece of verified advice. Each class
gets its own top-K, which is why this issues more than one index query -- a bounded
number (two in ``GUIDANCE``, four in the wider modes) rather than one per hit.

**Everything the formatter needs comes back from the index.** No per-hit trip to
SQLite: the projection carries the fields, and the only extra call is a single
batched source count for the handful of ids that survived ranking. Section 83 of
the round-6 brief calls the alternative an N+1 and it would be, twice over.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from aer.exceptions import KnowledgeError
from aer.knowledge import ranking
from aer.knowledge.base import IndexFilters, IndexMatch, IndexQuery, KnowledgeIndex
from aer.knowledge.models import (
    UNVERIFIED_STATUS_ORDER,
    VERIFIED_STATUS_ORDER,
    ExperienceSearchQuery,
    RetrievalHit,
    RetrievalResult,
)
from aer.runtime.enums import ExperienceKind, ExperienceStatus, RetrievalMode
from aer.runtime.serialization import utc_now

__all__ = ["ExperienceRetriever", "RetrievalPolicy"]

logger = logging.getLogger(__name__)

#: Kinds that can ever be guidance. ``FAILURE`` is not among them, by construction
#: rather than by a flag (round-6 brief, section 33).
_GUIDANCE_KINDS: tuple[str, ...] = (ExperienceKind.SUCCESS.value, ExperienceKind.RECOVERY.value)
_FAILURE_KINDS: tuple[str, ...] = (ExperienceKind.FAILURE.value,)

#: The status values that mean "this claim has been checked".
_VERIFIED_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in VERIFIED_STATUS_ORDER)
#: The status values that mean "a claim exists but nothing has checked it".
_UNVERIFIED_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in UNVERIFIED_STATUS_ORDER)
_DEPRECATED = ExperienceStatus.DEPRECATED.value


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """Which records a mode may see, expressed as index filters.

    A value object, not a service: it holds no state and performs no I/O, so the
    rules below can be read and tested without an index or a database.
    """

    mode: RetrievalMode
    include_deprecated: bool = False

    @property
    def wants_deprecated(self) -> bool:
        """Withdrawn knowledge appears only in ``ALL``, and only on request.

        Both conditions, because either alone is a footgun: an ``ALL`` query that
        quietly included deprecated advice would look exactly like a normal one,
        and an ``include_deprecated=True`` on a ``GUIDANCE`` query is a caller who
        has misunderstood what guidance promises.
        """
        return self.include_deprecated and self.mode is RetrievalMode.ALL

    @property
    def inspects_observations(self) -> bool:
        """Whether unverified records are allowed to appear at all."""
        return self.mode is not RetrievalMode.GUIDANCE

    def guidance_filters(self) -> IndexFilters:
        """Confirmed successes and recoveries -- the only things called advice."""
        return IndexFilters(
            kinds=_GUIDANCE_KINDS,
            statuses=self._statuses(_VERIFIED_STATUS_VALUES),
            outcome_verified=True,
        )

    def failure_filters(self) -> IndexFilters:
        """Failures of any status.

        Not narrowed to ``VERIFIED``: a failure whose outcome was confirmed and one
        that is merely on record are equally useful as "do not do this", and the
        label distinguishes them for the reader.
        """
        return IndexFilters(
            kinds=_FAILURE_KINDS,
            statuses=self._statuses(None),
            outcome_verified=None,
        )

    def observation_filters(self) -> tuple[IndexFilters, ...]:
        """Everything non-failure that is *not* guidance -- as pushed-down filters.

        The class is defined by complement, and the complement of
        ``status >= VERIFIED AND outcome_verified`` is a disjunction, which Cypher
        here cannot take as parameters. It is therefore split into the two cases
        that make it up, and each gets its own query:

        * a status below ``VERIFIED``, whatever the outcome flag says -- the normal
          case, and the one section 34 describes;
        * a status at or above ``VERIFIED`` whose outcome was never confirmed.

        The second cannot currently be produced -- ``mark_verified`` only promotes
        when the outcome is evidence-backed -- but writing the class as "whatever
        guidance is not" means a future change to the lifecycle shows up as an
        observation rather than as a record that quietly stops being retrievable.
        """
        return (
            IndexFilters(
                kinds=_GUIDANCE_KINDS,
                statuses=self._statuses(_UNVERIFIED_STATUS_VALUES),
                outcome_verified=None,
            ),
            IndexFilters(
                kinds=_GUIDANCE_KINDS,
                statuses=self._statuses(_VERIFIED_STATUS_VALUES),
                outcome_verified=False,
            ),
        )

    def _statuses(self, base: tuple[str, ...] | None) -> tuple[str, ...]:
        """``base`` (or every non-deprecated status), plus ``DEPRECATED`` on request."""
        if base is None:
            statuses = tuple(
                s.value for s in ExperienceStatus if s is not ExperienceStatus.DEPRECATED
            )
        else:
            statuses = base
        if self.wants_deprecated:
            return (*statuses, _DEPRECATED)
        return statuses


class ExperienceRetriever:
    """Answers a search query from the knowledge index, with policy and ranking."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        clock: object = None,
    ) -> None:
        self._index = index
        #: Injected so freshness can be tested without waiting six months.
        self._clock = clock if callable(clock) else utc_now

    @property
    def index(self) -> KnowledgeIndex:
        """The index behind this retriever."""
        return self._index

    def retrieve(self, query: ExperienceSearchQuery) -> RetrievalResult:
        """Run ``query`` and return guidance and warnings, each ranked.

        Raises:
            KnowledgeError: a name in the index did not decode into the current
                vocabulary. That means the projection was written by a different
                build, which is a rebuild, not a silently skipped row.
        """
        started = time.perf_counter()
        policy = RetrievalPolicy(mode=query.mode, include_deprecated=query.include_deprecated)
        limit = query.effective_limit

        guidance = self._rank(
            self._search(policy.guidance_filters(), query, limit),
            query,
            limit,
        )
        warnings = self._rank(self._warning_matches(policy, query, limit), query, limit)

        hits = guidance + warnings
        if hits:
            counts = self._source_counts(tuple(hit.experience_id for hit in hits))
            guidance = tuple(_with_source_count(hit, counts) for hit in guidance)
            warnings = tuple(_with_source_count(hit, counts) for hit in warnings)

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "retrieval query=%r domain=%s mode=%s guidance=%d warnings=%d %.1fms",
            query.query,
            query.domain,
            query.mode.value,
            len(guidance),
            len(warnings),
            elapsed_ms,
        )

        return RetrievalResult(
            query=query.query,
            domain=query.domain,
            mode=query.mode,
            guidance=guidance,
            warnings=warnings,
        )

    # -- internals ---------------------------------------------------------

    def _warning_matches(
        self,
        policy: RetrievalPolicy,
        query: ExperienceSearchQuery,
        limit: int,
    ) -> list[IndexMatch]:
        """Failures plus, in the wider modes, unverified observations.

        Failures are always included: a caller asking for guidance about a problem
        is exactly the caller who benefits from knowing the problem has already
        defeated someone.
        """
        matches = self._search(policy.failure_filters(), query, limit)
        if policy.inspects_observations:
            for filters in policy.observation_filters():
                matches.extend(self._search(filters, query, limit))
        return matches

    def _search(
        self,
        filters: IndexFilters,
        query: ExperienceSearchQuery,
        limit: int,
    ) -> list[IndexMatch]:
        """One index query. Separated so tests can count the round trips."""
        return self._index.search(
            IndexQuery(
                text=query.query,
                filters=filters,
                limit=limit,
                domain=query.domain,
            )
        )

    def _rank(
        self,
        matches: list[IndexMatch],
        query: ExperienceSearchQuery,
        limit: int,
    ) -> tuple[RetrievalHit, ...]:
        """Score, order and truncate one class of matches.

        The sort key is ``(-retrieval_score, experience_id)``. The id is not
        decoration: two experiences with identical text and identical trust would
        otherwise come back in whatever order the engine felt like, and a rebuild
        that reorders identical results makes the drill in section 94 impossible to
        interpret.
        """
        now = self._clock()
        if not isinstance(now, datetime):
            raise KnowledgeError(f"clock returned {type(now).__name__}, expected datetime")
        hits = [
            _to_hit(match, query_domain=query.domain, now=now)
            for match in matches
            if match.experience_id
        ]
        hits.sort(key=lambda hit: (-hit.retrieval_score, hit.experience_id))
        return tuple(hits[:limit])

    def _source_counts(self, experience_ids: tuple[str, ...]) -> dict[str, int]:
        """One batched call for every id that survived ranking."""
        if not experience_ids:
            return {}
        return self._index.source_counts(experience_ids)


def _to_hit(match: IndexMatch, *, query_domain: str | None, now: datetime) -> RetrievalHit:
    """Decode one index row into a scored hit."""
    kind = _decode_kind(match)
    status = _decode_status(match)
    created_at = _parse_timestamp(match.created_at, match.experience_id, "created_at")
    updated_at = _parse_timestamp(match.updated_at, match.experience_id, "updated_at")

    text = ranking.text_score(match.bm25_score)
    trust = ranking.trust_score(status, outcome_verified=match.outcome_verified)
    domain = ranking.domain_score(match.domain, query_domain)
    freshness = ranking.freshness_score(updated_at, now=now)

    return RetrievalHit(
        experience_id=match.experience_id,
        kind=kind,
        status=status,
        domain=match.domain,
        title=match.title,
        problem=match.problem,
        root_cause=match.root_cause or None,
        solution=match.solution or None,
        failed_attempts=_split_lines(match.failed_attempts_text),
        avoid=_split_lines(match.avoid_text),
        outcome_verified=match.outcome_verified,
        generalizable=match.generalizable,
        source_count=0,
        created_at=created_at,
        updated_at=updated_at,
        bm25_score=match.bm25_score,
        text_score=text,
        trust_score=trust,
        domain_score=domain,
        freshness_score=freshness,
        retrieval_score=ranking.combine(text=text, trust=trust, domain=domain, freshness=freshness),
    )


def _with_source_count(hit: RetrievalHit, counts: dict[str, int]) -> RetrievalHit:
    """Return ``hit`` carrying its source count.

    ``RetrievalHit`` is frozen, so this rebuilds rather than mutates: a hit handed
    to a caller never changes under it.
    """
    return hit.model_copy(update={"source_count": counts.get(hit.experience_id, 0)})


def _split_lines(value: str) -> tuple[str, ...]:
    """Recover a list field from the projection's newline-joined form."""
    return tuple(part for part in (line.strip() for line in value.split("\n")) if part)


def _decode_kind(match: IndexMatch) -> ExperienceKind:
    try:
        return ExperienceKind(match.kind)
    except ValueError as exc:
        raise KnowledgeError(
            f"Knowledge index holds kind {match.kind!r} for experience "
            f"{match.experience_id}, which this build does not know. The projection "
            "was written by a different version; rebuild it."
        ) from exc


def _decode_status(match: IndexMatch) -> ExperienceStatus:
    try:
        return ExperienceStatus(match.status)
    except ValueError as exc:
        raise KnowledgeError(
            f"Knowledge index holds status {match.status!r} for experience "
            f"{match.experience_id}, which this build does not know. The projection "
            "was written by a different version; rebuild it."
        ) from exc


def _parse_timestamp(raw: str, experience_id: str, field: str) -> datetime:
    """Parse a projected ISO timestamp, insisting on a timezone.

    A naive value would be silently interpreted as local time by anything that
    later compares it to ``utc_now()``, which turns a freshness score into a
    function of the server's locale.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise KnowledgeError(
            f"Knowledge index holds {field}={raw!r} for experience {experience_id}, "
            "which is not an ISO-8601 timestamp. Rebuild the projection."
        ) from exc
    if parsed.tzinfo is None:
        raise KnowledgeError(
            f"Knowledge index holds a timezone-naive {field} for experience "
            f"{experience_id}; AER stores timezone-aware UTC everywhere. Rebuild."
        )
    return parsed.astimezone(UTC)
