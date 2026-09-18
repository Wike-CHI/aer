"""The retrieval vocabulary: what a query is, what a hit is, what an answer is.

These are the only types that cross out of the knowledge plane into an agent's
code, so they are deliberately explicit. A hit carries the *evidence* for its own
trustworthiness -- kind, status, whether the outcome was confirmed, how many runs
it came from -- instead of a single opaque score, because the whole point of this
milestone is that a caller can tell "this worked, and we checked" apart from "this
is something that once happened".

The label each hit wears is derived here, in one place, from the record. If the
labels lived in the formatter instead, a second consumer (a dashboard, a log line)
would invent its own, and the first thing to drift would be whether the word
"Proven" is allowed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from aer.exceptions import KnowledgeQueryError
from aer.runtime.enums import ExperienceKind, ExperienceStatus, RetrievalMode

__all__ = [
    "DEFAULT_RETRIEVAL_LIMIT",
    "KNOWN_FAILURE_LABEL",
    "MAX_RETRIEVAL_LIMIT",
    "MIN_RETRIEVAL_LIMIT",
    "UNVERIFIED_RECOVERY_LABEL",
    "UNVERIFIED_STATUS_ORDER",
    "UNVERIFIED_SUCCESS_LABEL",
    "VERIFIED_RECOVERY_LABEL",
    "VERIFIED_STATUSES",
    "VERIFIED_STATUS_ORDER",
    "VERIFIED_SUCCESS_LABEL",
    "ExperienceSearchQuery",
    "RetrievalHit",
    "RetrievalResult",
]

#: Default number of experiences handed to an agent.
DEFAULT_RETRIEVAL_LIMIT = 3
#: Anything below one is a caller bug, not a request for nothing.
MIN_RETRIEVAL_LIMIT = 1
#: Hard ceiling. agent.md #31 caps injection at five top-5 on purpose: retrieval
#: that fills the context stops being help and starts being noise.
MAX_RETRIEVAL_LIMIT = 5

#: Statuses that count as "the claim itself has been checked".
#:
#: Derived from the lifecycle's declaration order rather than hard-coded, so that
#: adding a status to ``ExperienceStatus`` cannot silently leave this set behind.
#: ``DEPRECATED`` is excluded explicitly: it is declared last but it is not the
#: *most* advanced state, it is the withdrawn one.
_ORDERED_STATUSES = tuple(
    status for status in ExperienceStatus if status is not ExperienceStatus.DEPRECATED
)
#: Statuses at or above ``VERIFIED``, in lifecycle order.
VERIFIED_STATUS_ORDER: tuple[ExperienceStatus, ...] = _ORDERED_STATUSES[
    _ORDERED_STATUSES.index(ExperienceStatus.VERIFIED) :
]
#: Statuses below ``VERIFIED``: a claim exists but nothing has checked it.
#:
#: Together with :data:`VERIFIED_STATUS_ORDER` this partitions the non-deprecated
#: lifecycle, which is what lets the retriever express "not verified" as a
#: pushed-down filter instead of a Python pass over an arbitrarily truncated result.
UNVERIFIED_STATUS_ORDER: tuple[ExperienceStatus, ...] = _ORDERED_STATUSES[
    : _ORDERED_STATUSES.index(ExperienceStatus.VERIFIED)
]
VERIFIED_STATUSES: frozenset[ExperienceStatus] = frozenset(VERIFIED_STATUS_ORDER)

VERIFIED_SUCCESS_LABEL = "Verified Success"
VERIFIED_RECOVERY_LABEL = "Verified Recovery"
KNOWN_FAILURE_LABEL = "Known Failure"
UNVERIFIED_SUCCESS_LABEL = "Unverified Success Observation"
UNVERIFIED_RECOVERY_LABEL = "Unverified Recovery Observation"

_FROZEN = ConfigDict(frozen=True)


class ExperienceSearchQuery(BaseModel):
    """What the caller asked for.

    Frozen, and validated on construction, because a query object that can be
    edited after the policy has read it is a query whose policy no longer
    describes it.

    ``limit`` is **clamped** into ``[MIN, MAX]`` rather than rejected. The ceiling
    exists to protect an agent's context window, and a caller asking for 50
    experiences has made a sizing mistake, not a semantic one -- refusing the
    request would leave them with no retrieval at all. An empty ``query`` is a
    different matter and does raise: there is no sensible correction for "search
    for nothing", and quietly returning arbitrary rows would look like an answer.
    """

    query: str
    domain: str | None = None
    mode: RetrievalMode = RetrievalMode.GUIDANCE
    limit: int = DEFAULT_RETRIEVAL_LIMIT
    include_deprecated: bool = False
    """Only consulted in :attr:`RetrievalMode.ALL`; deprecated knowledge never
    leaks into a narrower mode by accident."""

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if not self.query.strip():
            raise KnowledgeQueryError(
                "Retrieval needs a non-empty query: an empty search cannot be "
                "answered, and returning rows anyway would look like one."
            )

    @property
    def effective_limit(self) -> int:
        """``limit`` pinned into the allowed range."""
        return max(MIN_RETRIEVAL_LIMIT, min(self.limit, MAX_RETRIEVAL_LIMIT))

    @property
    def wants_deprecated(self) -> bool:
        """Whether withdrawn knowledge may appear in the answer."""
        return self.include_deprecated and self.mode is RetrievalMode.ALL


class RetrievalHit(BaseModel):
    """One experience offered back to an agent, with its trust evidence attached."""

    model_config = _FROZEN

    experience_id: str
    kind: ExperienceKind
    status: ExperienceStatus
    domain: str

    title: str
    problem: str
    root_cause: str | None
    solution: str | None
    failed_attempts: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    outcome_verified: bool
    generalizable: bool
    source_count: int

    created_at: datetime
    updated_at: datetime

    bm25_score: float
    """Raw engine output. Kept for debugging: *lower is better* here, unlike
    every other score on this object."""

    text_score: float
    trust_score: float
    domain_score: float
    freshness_score: float
    retrieval_score: float

    @property
    def is_verified(self) -> bool:
        """Whether this can be presented as something that is known to work.

        Two independent conditions, both required: the record has been through
        verification, *and* the outcome itself was confirmed. Either one alone is
        not enough -- a ``VERIFIED`` claim about an unconfirmed outcome is a
        verified statement, not verified knowledge.
        """
        return self.status in VERIFIED_STATUSES and self.outcome_verified

    @property
    def is_deprecated(self) -> bool:
        """Whether this knowledge has been withdrawn."""
        return self.status is ExperienceStatus.DEPRECATED

    @property
    def label(self) -> str:
        """The role this hit plays, as it must be shown to a reader.

        Order matters. Deprecation is checked first because it overrides every
        other claim: a withdrawn solution is not a verified solution. A
        ``FAILURE`` is checked next because it is never guidance of any kind --
        presenting it as one is the single most damaging thing this milestone
        could do, so the branch is structural rather than a flag someone can flip.
        """
        if self.is_deprecated:
            return "Deprecated"
        if self.kind is ExperienceKind.FAILURE:
            return KNOWN_FAILURE_LABEL
        if not self.is_verified:
            return (
                UNVERIFIED_SUCCESS_LABEL
                if self.kind is ExperienceKind.SUCCESS
                else UNVERIFIED_RECOVERY_LABEL
            )
        return (
            VERIFIED_SUCCESS_LABEL
            if self.kind is ExperienceKind.SUCCESS
            else VERIFIED_RECOVERY_LABEL
        )


class RetrievalResult(BaseModel):
    """Two lists, not one ranked list, because the roles are not symmetric.

    ``guidance`` is what a caller may act on. ``warnings`` is what a caller must
    not -- confirmed failures and unverified observations, which are useful
    precisely because they say what *did not* work. Flattening them into one list
    with a flag would put the decision of "is this advice" one careless sort away
    from being lost.
    """

    model_config = _FROZEN

    query: str
    domain: str | None
    mode: RetrievalMode

    guidance: tuple[RetrievalHit, ...] = ()
    warnings: tuple[RetrievalHit, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether retrieval found nothing at all.

        Meaningfully different from "the index was unreachable", which raises
        :class:`~aer.exceptions.KnowledgeIndexUnavailable` instead of returning an
        empty result (round-6 brief, section 80).
        """
        return not self.guidance and not self.warnings

    @property
    def all_hits(self) -> tuple[RetrievalHit, ...]:
        """Every hit, guidance first, order preserved within each group."""
        return self.guidance + self.warnings
