"""Recording what actually happened to a retrieval.

This module is the answer to the question the whole milestone exists for: an
experience was retrieved -- and then what? Five states are possible and the code keeps
them strictly apart (round-7 brief, section 2)::

    Retrieved  ->  Injected  ->  Adopted / Ignored / Rejected
    (a search)     (in context)     (explicit judgement)

Two rules govern every write here.

**Nothing is inferred.** A retrieval result is a list of candidates. No amount of
text similarity between an experience and a later tool call turns ``UNKNOWN`` into
``ADOPTED`` (section 22); only a caller that knows may say so, and it has to say
where it knows it from.

**Tracking is explicit.** :meth:`~aer.runtime.runtime.AER.retrieve` stays a pure
function. If every search recorded usage, a dashboard query, a debug session and a
test run would all inflate the numbers the effectiveness report is supposed to
trust (section 17). Only :meth:`ExperienceUsageService.retrieve_for_run` writes.

The failure boundary is deliberate too. Retrieval happens first and records nothing;
if the tracking write then fails, the caller gets
:class:`~aer.exceptions.UsageTrackingError` -- never a silent "tracked" answer
(section 51), and never a rolled-back or modified retrieval result (section 51 again:
the knowledge index is not ours to touch here, and section 4 keeps it read-only).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from time import perf_counter

from aer.exceptions import RecordNotFoundError, StorageError, UsageTrackingError
from aer.knowledge.models import (
    DEFAULT_RETRIEVAL_LIMIT,
    ExperienceSearchQuery,
    RetrievalHit,
    RetrievalResult,
)
from aer.knowledge.retriever import RETRIEVAL_POLICY_VERSION, ExperienceRetriever
from aer.runtime.enums import (
    ExperienceKind,
    RetrievalMode,
    SessionAssignment,
    UsageRole,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)
from aer.runtime.models import ExperienceUsage, RetrievalSession
from aer.runtime.serialization import to_json_object, utc_now
from aer.storage.repositories import (
    ExperienceUsageRepository,
    RetrievalSessionRepository,
    RunRepository,
)
from aer.usage.fingerprints import query_fingerprint, sanitize_query

__all__ = ["ExperienceUsageService", "TrackedRetrievalResult"]


@dataclass(frozen=True, slots=True)
class TrackedRetrievalResult:
    """A retrieval that was recorded, plus the answer it produced.

    The two fields are exactly the two things a caller needs and cannot derive: the
    id to record injections and signals against, and the result to render. The
    session itself is reloadable by id, so it is not duplicated here.
    """

    session_id: str
    result: RetrievalResult

    @property
    def is_empty(self) -> bool:
        """Whether the recorded retrieval found nothing at all.

        A recorded empty result is a legitimate outcome, not a failure to track
        (section 7): the session says a search happened and the knowledge base had
        nothing to offer.
        """
        return self.result.is_empty


class ExperienceUsageService:
    """Writes the usage facts: sessions, injections, signals and utility labels."""

    def __init__(
        self,
        *,
        retriever: ExperienceRetriever,
        sessions: RetrievalSessionRepository,
        usage: ExperienceUsageRepository,
        runs: RunRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._retriever = retriever
        self._sessions = sessions
        self._usage = usage
        self._runs = runs
        self._clock = clock if callable(clock) else utc_now

    # -- retrieval ---------------------------------------------------------

    def retrieve_for_run(
        self,
        query: str,
        *,
        run_id: str | None = None,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = DEFAULT_RETRIEVAL_LIMIT,
        include_deprecated: bool = False,
        experiment_id: str | None = None,
        assignment: SessionAssignment = SessionAssignment.NONE,
        metadata: Mapping[str, object] | None = None,
    ) -> TrackedRetrievalResult:
        """Retrieve, and record that the retrieval happened.

        The tracked counterpart of :meth:`~aer.runtime.runtime.AER.retrieve`. Same
        answer, same ranking, same policy -- plus one row in ``retrieval_sessions``
        and one usage row per experience returned.

        Args:
            query: The retrieval question. What is *stored* is the sanitised form
                (section 6); what is *searched* is the caller's text unchanged, so
                that the tracked and untracked paths cannot disagree about results.
            run_id: The run this retrieval is for. Optional, because a retrieval may
                precede the run it informs (section 24); use
                :meth:`attach_session_to_run` to link it afterwards. When given, the
                run must exist.
            domain / mode / limit / include_deprecated: as for ``retrieve``.
            experiment_id / assignment: reserved for the holdout design of section 32.

        Returns:
            The session id and the untouched retrieval result.

        Raises:
            RecordNotFoundError: ``run_id`` names no known run.
            KnowledgeIndexUnavailable: the index cannot be reached. Reported as
                itself rather than as an empty result (section 85).
            KnowledgeQueryError: the query has no searchable text.
            UsageTrackingError: retrieval succeeded but the usage facts could not be
                written. The caller must not treat this retrieval as tracked.
        """
        search = ExperienceSearchQuery(
            query=query,
            domain=domain,
            mode=mode,
            limit=limit,
            include_deprecated=include_deprecated,
        )
        if run_id is not None and self._runs.get(run_id) is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")

        started = perf_counter()
        result = self._retriever.retrieve(search)
        duration_ms = int((perf_counter() - started) * 1000)

        sanitized = sanitize_query(query)
        session = RetrievalSession(
            run_id=run_id,
            query_text=sanitized,
            query_fingerprint=query_fingerprint(sanitized),
            domain=domain,
            mode=mode,
            requested_limit=search.effective_limit,
            result_count=len(result.all_hits),
            knowledge_projection_version=self._retriever.index.projection_version(),
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
            retrieval_duration_ms=duration_ms,
            experiment_id=experiment_id,
            assignment=assignment,
            metadata=to_json_object(metadata),
        )
        usages = [
            ExperienceUsage(
                retrieval_session_id=session.id,
                experience_id=hit.experience_id,
                rank=rank,
                role=role,
                retrieval_score=hit.retrieval_score,
                retrieved_at=session.created_at,
            )
            for rank, role, hit in _result_entries(result)
        ]

        try:
            self._sessions.create(session, usages=usages)
        except StorageError as exc:
            raise UsageTrackingError(
                f"Retrieval for run {run_id or '<unattached>'} succeeded "
                f"({len(usages)} result(s)) but recording the retrieval session failed: "
                f"{exc}. This retrieval is NOT tracked."
            ) from exc

        return TrackedRetrievalResult(session_id=session.id, result=result)

    # -- injection ---------------------------------------------------------

    def record_injection(
        self,
        *,
        session_id: str,
        experience_ids: Sequence[str],
        context_fingerprint: str | None = None,
        formatter_version: str | None = None,
        positions: Mapping[str, int] | None = None,
        char_counts: Mapping[str, int] | None = None,
        injected_at: datetime | None = None,
    ) -> tuple[ExperienceUsage, ...]:
        """Record that these experiences entered the agent's context.

        Only a subset of a result may be injected -- a caller that renders the top
        two of five experiences records two (section 20) -- but an experience that
        was **not** in the result cannot be recorded at all, because a usage row for
        something nobody retrieved is not evidence, it is a fabrication.

        Recording the same injection twice is a no-op: a context rendered twice is
        still one injection (section 52). A second call that contradicts the first
        -- a different context fingerprint for the same experience in the same
        session -- is refused rather than overwritten.

        Args:
            session_id: The session whose result was injected.
            experience_ids: Which of its experiences were actually placed in the
                context.
            context_fingerprint: Digest of the rendered context. The text itself is
                never stored (section 19).
            formatter_version: Which renderer produced it
                (:data:`~aer.knowledge.formatter.FORMATTER_VERSION` for the built-in
                one). Recorded so that a later formatter change is not invisible.
            positions: Optional ``experience_id -> position`` map, usually the
                experience's rank in the rendered context.
            char_counts: Optional ``experience_id -> characters`` map. Compute it
                with :meth:`~aer.knowledge.formatter.ExperienceContextFormatter.format_hit`
                rather than by dividing a total.
            injected_at: When the context was built. Defaults to now.

        Returns:
            One row per requested experience, in the order requested, reflecting the
            state after the call.

        Raises:
            RecordNotFoundError: the session does not exist.
            UsageTrackingError: an experience was not part of this session, a
                mapping names an unrelated experience, the same experience was named
                twice, an existing injection contradicts this one, or the write
                failed. Nothing is written when any of those hold.
        """
        requested = list(experience_ids)
        if not requested:
            raise UsageTrackingError(
                "record_injection needs at least one experience id; an empty list "
                "cannot be distinguished from a call that forgot to pass one"
            )
        duplicates = sorted({item for item in requested if requested.count(item) > 1})
        if duplicates:
            raise UsageTrackingError(
                f"record_injection received {duplicates} more than once; an "
                "experience appears at most once per retrieval session"
            )
        self._require_session(session_id)
        _require_known_keys("positions", positions, requested)
        _require_known_keys("char_counts", char_counts, requested)

        current = {row.experience_id: row for row in self._usage.get_by_session(session_id)}
        missing = [experience_id for experience_id in requested if experience_id not in current]
        if missing:
            raise UsageTrackingError(
                f"Experience(s) {missing} are not part of retrieval session {session_id}; "
                "only an experience that was actually returned can be recorded as "
                "injected (section 20)"
            )

        stamp = injected_at or self._clock()
        resolved: dict[str, ExperienceUsage] = {}
        pending: list[ExperienceUsage] = []
        for experience_id in requested:
            row = current[experience_id]
            if row.is_injected:
                if (
                    context_fingerprint is not None
                    and row.context_fingerprint is not None
                    and context_fingerprint != row.context_fingerprint
                ):
                    raise UsageTrackingError(
                        f"Experience {experience_id} is already recorded as injected in "
                        f"session {session_id} with a different context fingerprint. "
                        "Refusing to overwrite an existing injection record."
                    )
                resolved[experience_id] = row
                continue
            pending.append(
                row.with_injection(
                    injected_at=stamp,
                    context_fingerprint=context_fingerprint,
                    formatter_version=formatter_version,
                    injection_position=None if positions is None else positions.get(experience_id),
                    injection_chars=None if char_counts is None else char_counts.get(experience_id),
                )
            )

        if pending:
            try:
                self._usage.update_many(pending)
            except StorageError as exc:
                raise UsageTrackingError(
                    f"Recording the injection for session {session_id} failed: {exc}. "
                    "No injection state was changed."
                ) from exc
            resolved.update({row.experience_id: row for row in pending})

        return tuple(resolved[experience_id] for experience_id in requested)

    # -- usage signal ------------------------------------------------------

    def record_usage_signal(
        self,
        *,
        session_id: str,
        experience_id: str,
        signal: UsageSignal,
        source: UsageSignalSource | None = None,
        at: datetime | None = None,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether the agent adopted, ignored or rejected an experience.

        Transitions follow section 53: ``UNKNOWN`` may become anything, the same
        value may be written again (a no-op), and a decided value may only be changed
        with ``override=True``. A silent flip would destroy the distinction the
        report is built on -- "the agent ignored this" quietly becoming "the agent
        adopted this" is the single most corrupting write this table can receive.

        Args:
            session_id / experience_id: which row to update. The pair must exist.
            signal: The new signal.
            source: Who says so. Required whenever ``signal`` is not ``UNKNOWN``.
            at: When the signal was asserted. Defaults to now.
            override: Explicitly replace an existing, different signal.
            metadata: Extra JSON, merged into the row's metadata.

        Raises:
            RecordNotFoundError: the session, or the row, does not exist.
            UsageTrackingError: the signal and its source disagree, or the change
                contradicts an existing signal without ``override``.
        """
        signal = UsageSignal(signal)
        source_enum = _require_signal_source(signal, source)
        row = self._require_usage(session_id, experience_id)
        if not _change_is_allowed(
            current=row.usage_signal,
            requested=signal,
            unknown=UsageSignal.UNKNOWN,
            override=override,
            what=f"Usage signal for experience {experience_id} in session {session_id}",
        ):
            return row
        return self._usage.update(
            row.with_usage_signal(
                signal=signal,
                source=source_enum,
                at=at or self._clock(),
                metadata=to_json_object(metadata),
            )
        )

    # -- utility -----------------------------------------------------------

    def record_utility(
        self,
        *,
        session_id: str,
        experience_id: str,
        label: UtilityLabel,
        source: UtilitySource | None = None,
        at: datetime | None = None,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether an experience helped, once that can be judged.

        Deliberately separate from :meth:`record_usage_signal`, and deliberately
        never derived from it: ``ADOPTED`` is what the agent did, ``HELPFUL`` is what
        happened. An agent can adopt an experience that is wrong, and a task can
        succeed while the experience was ignored (section 12, section 14).

        Transitions and validation are identical to :meth:`record_usage_signal`; see
        there for the ``override`` semantics and the errors raised.
        """
        label = UtilityLabel(label)
        source_enum = _require_utility_source(label, source)
        row = self._require_usage(session_id, experience_id)
        if not _change_is_allowed(
            current=row.utility_label,
            requested=label,
            unknown=UtilityLabel.UNKNOWN,
            override=override,
            what=f"Utility label for experience {experience_id} in session {session_id}",
        ):
            return row
        return self._usage.update(
            row.with_utility(
                label=label,
                source=source_enum,
                at=at or self._clock(),
                metadata=to_json_object(metadata),
            )
        )

    # -- association -------------------------------------------------------

    def attach_session_to_run(
        self,
        session_id: str,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> RetrievalSession:
        """Link a session that was recorded before its run existed.

        Idempotent for the same run, and refuses to re-point an attached session
        (section 24): every usage row under it would otherwise be reattributed to a
        task it never informed.

        Raises:
            RecordNotFoundError: the session or the run does not exist.
            UsageTrackingError: the session is attached to a different run.
        """
        session = self._require_session(session_id)
        if self._runs.get(run_id) is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        attached = session.attach_run(run_id, reason=reason)
        if attached is session:
            return session
        return self._sessions.update(attached)

    # -- internals ---------------------------------------------------------

    def _require_session(self, session_id: str) -> RetrievalSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise RecordNotFoundError(f"Retrieval session not found: {session_id}")
        return session

    def _require_usage(self, session_id: str, experience_id: str) -> ExperienceUsage:
        self._require_session(session_id)
        row = self._usage.get_by_session_and_experience(session_id, experience_id)
        if row is None:
            raise UsageTrackingError(
                f"Experience {experience_id} is not part of retrieval session "
                f"{session_id}; only an experience that was actually returned can be "
                "tracked (section 20)"
            )
        return row


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _result_entries(
    result: RetrievalResult,
) -> tuple[tuple[int, UsageRole, RetrievalHit], ...]:
    """Number the result's hits the way the agent sees them.

    Rank runs across the whole result, guidance first, which is the order the
    formatter renders. Warnings are split by kind: a confirmed failure and an
    unverified observation are different kinds of caution, and one list here would be
    indistinguishable in the report (section 9).
    """
    entries: list[tuple[int, UsageRole, RetrievalHit]] = []
    rank = 0
    for hit in result.guidance:
        rank += 1
        entries.append((rank, UsageRole.GUIDANCE, hit))
    for hit in result.warnings:
        rank += 1
        role = UsageRole.WARNING if hit.kind is ExperienceKind.FAILURE else UsageRole.OBSERVATION
        entries.append((rank, role, hit))
    return tuple(entries)


def _require_signal_source(
    signal: UsageSignal,
    source: UsageSignalSource | None,
) -> UsageSignalSource | None:
    """Check that a usage signal and its provenance arrive together.

    ``UNKNOWN`` means "nobody said anything", so it carries no source; anything else
    is an assertion and is worthless without one. Rejected here rather than only at
    the model layer so the caller sees a usage error with the right family.
    """
    _require_paired(what="usage_signal", value=signal.value, source=source, unknown="UNKNOWN")
    return None if source is None else UsageSignalSource(source)


def _require_utility_source(
    label: UtilityLabel,
    source: UtilitySource | None,
) -> UtilitySource | None:
    """Check that a utility label and its provenance arrive together."""
    _require_paired(what="utility_label", value=label.value, source=source, unknown="UNKNOWN")
    return None if source is None else UtilitySource(source)


def _require_paired(*, what: str, value: str, source: object, unknown: str) -> None:
    decided = value != unknown
    if decided and source is None:
        raise UsageTrackingError(
            f"{what}={value} requires a source: an unsourced claim cannot be audited, "
            "and section 11 requires the provenance of every signal to be recorded"
        )
    if not decided and source is not None:
        raise UsageTrackingError(
            f"{what}={value} must not carry a source: {unknown} means no evidence "
            "exists, so there is nothing to attribute"
        )


def _change_is_allowed(
    *,
    current: Enum,
    requested: Enum,
    unknown: Enum,
    override: bool,
    what: str,
) -> bool:
    """Whether a recorded value may move to ``requested``.

    Returns ``False`` for a redundant write (same value), which keeps repeats
    idempotent. Raises for a contradiction, because the alternative -- last write
    wins -- is how an evidence table stops being evidence (section 53).
    """
    if current is requested:
        return False
    if current is unknown or override:
        return True
    raise UsageTrackingError(
        f"{what} is already recorded as {current.value} and cannot be changed to "
        f"{requested.value} without override=True. Contradictory evidence must be "
        "resolved explicitly, never by whichever write happened last."
    )


def _require_known_keys(
    name: str,
    mapping: Mapping[str, int] | None,
    requested: Sequence[str],
) -> None:
    """Refuse a mapping that names an experience this call does not concern.

    A stray key is a caller bug that would otherwise be dropped in silence -- and a
    silently dropped character count reads later as "this was injected with no
    measured size".
    """
    if mapping is None:
        return
    unknown = sorted(set(mapping) - set(requested))
    if unknown:
        raise UsageTrackingError(
            f"{name} names experience(s) {unknown} that are not in experience_ids; "
            "refusing to record detail for a row this call is not writing"
        )
