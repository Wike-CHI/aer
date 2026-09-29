"""SQLAlchemy ORM tables for the AER SQLite store (Milestone 2 Task 2.2, M3).

Realised tables:

* ``runs``          -- one row per agent task execution
* ``events``        -- one row per standardised runtime event
* ``errors``        -- structured failures (Milestone 3)
* ``recoveries``    -- recovery attempts, optionally bound to an error (Milestone 3)
* ``verifications`` -- independent verdicts on a run's outcome (Milestone 4)
* ``experiences``   -- distilled, reusable knowledge (Milestone 5)
* ``experience_sources`` -- which runs support an experience (Milestone 5)
* ``retrieval_sessions`` -- one retrieval, recorded as a fact (Milestone 7)
* ``experience_usage`` -- one experience offered to one retrieval session (Milestone 7)
* ``adapter_sessions`` -- an external Agent session mapped to one AER run (Milestone 8)
* ``adapter_events`` -- the idempotency ledger for external events (Milestone 8)

Still pending from the agreed MVP schema: ``artifacts``, ``workflows`` and
``dataset_items``. Their SQL is already agreed in the design document, so adding
them later is additive work; creating empty tables now would only add untested
surface area.

Why the usage tables live **here** rather than in the knowledge index: they record
what an agent did, not what is known. They are behaviour facts, they are queried as
facts, and the knowledge index is a disposable projection of a different question
(round-7 brief, sections 3-4).

Storage-format conventions:

* timestamps -> :class:`UTCDateTime` (naive UTC on the wire, timezone-aware in
  Python);
* free-form payloads -> ``*_json`` TEXT columns; the encoding lives in
  :mod:`aer.storage.converters`, never in the domain models;
* booleans -> SQLAlchemy :class:`~sqlalchemy.Boolean`, which SQLite stores as
  INTEGER 0/1 (equivalent to the ``INTEGER`` the design document specifies).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Declarative base shared by every AER table."""


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware ``datetime`` column stored in SQLite as ``DATETIME``.

    SQLite has no timezone support at all. This decorator normalises values to
    UTC when writing and re-attaches ``UTC`` when reading, so the domain layer
    never observes a naive datetime regardless of the host's local timezone.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class RunRow(Base):
    """``runs`` table -- see agent.md #10 for the agreed column set."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    task_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_description: Mapped[str] = mapped_column(Text, nullable=False)

    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_runs_started_at", "started_at"),
        Index("ix_runs_status", "status"),
    )


class EventRow(Base):
    """``events`` table -- ordered by ``sequence`` inside a run (agent.md #11)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Enforces "sequence is strictly unique per run" at the database level,
        # so a concurrency bug can never silently produce a corrupted trace.
        UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
        Index("ix_events_run_sequence", "run_id", "sequence"),
        # Match the agreed schema exactly: INTEGER PRIMARY KEY AUTOINCREMENT.
        # Note this also prevents rowid reuse, which matters for an audit store.
        {"sqlite_autoincrement": True},
    )


class ErrorRow(Base):
    """``errors`` table -- structured failures (added in Milestone 3).

    Failures are stored as rows rather than only inside an event payload because
    failure analysis, recovery retrieval and experience distillation all need to
    query them (kind, recoverability, resolution state) without parsing JSON.
    """

    __tablename__ = "errors"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The ERROR event describing this failure. Nullable because an error can be
    # imported or backfilled without an event; ON DELETE SET NULL keeps the error
    # itself alive if the event is pruned.
    event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )

    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[str | None] = mapped_column(Text, nullable=True)

    recoverable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_errors_run_id", "run_id"),
        Index("ix_errors_resolved", "resolved"),
    )


class RecoveryRow(Base):
    """``recoveries`` table -- a failed attempt followed by a repair attempt.

    Recovery trajectories are the highest-value distillation input (agent.md #32),
    so the links back to the originating error and to the two events that bracket
    the attempt are explicit columns rather than JSON.
    """

    __tablename__ = "recoveries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The error this attempt is trying to clear. NULL means "a recovery without a
    # known target" -- allowed, but such a recovery can never resolve anything.
    error_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("errors.id", ondelete="SET NULL"),
        nullable=True,
    )
    start_event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    result_event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL while the attempt is still open; True/False once RECOVERY_RESULT lands.
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    outcome_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_recoveries_run_id", "run_id"),
        Index("ix_recoveries_error_id", "error_id"),
    )


class VerificationRow(Base):
    """``verifications`` table -- independent verdicts on a run's outcome.

    Verdicts are rows rather than event payloads because they are *queried* as
    facts: how many required checks failed, which verifier disagreed with the
    agent, whether a run ever earned a verified success. Parsing that out of JSON
    in the ``events`` table would make the central distinction of this milestone
    ("agent SUCCESS != verified SUCCESS") a string search.

    ``required`` is stored (rather than inferred from the verifier name) so the
    promotion rule for a verified success is readable directly from the schema --
    and so it stays stable even if a verifier's defaults change later.
    """

    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The VERIFICATION event that carried this verdict. Nullable for the same
    # reason as errors.event_id: a verdict can be imported or backfilled.
    event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )

    verifier_type: Mapped[str] = mapped_column(Text, nullable=False)
    verifier_name: Mapped[str] = mapped_column(Text, nullable=False)

    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # NULL means "this verifier produced no graded quality", not "score 0.0".
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_verifications_run_id", "run_id"),)


class ExperienceRow(Base):
    """``experiences`` table -- distilled knowledge, per agent.md #21.

    Differences from the schema in the design document, all recorded as decisions:

    * ``kind``, ``generalizable``, ``outcome_verified`` and ``dedup_key`` are new
      (round-5 brief sections 3, 11, 39);
    * ``outcome_verified`` is a column rather than something derived, because for a
      ``FAILURE`` experience "the failure really happened" is the *only* verified
      fact, and a reader must not have to infer whether it held;
    * ``reuse_count`` / ``success_count`` / ``failure_count`` are deliberately
      **absent**: they are outputs of ``experience_usage``, which does not exist
      yet, and three counters that nothing increments are worse than no counters
      (they read as "never reused"). They arrive with Milestone 7 (D-037).
    """

    __tablename__ = "experiences"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    problem: Mapped[str] = mapped_column(Text, nullable=False)

    symptoms_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL is a legitimate value, not a gap: an unresolved failure has no known
    # root cause or solution, and inventing one to satisfy the schema would turn a
    # hypothesis into a stored fact (round-5 brief, section 6).
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    solution: Mapped[str | None] = mapped_column(Text, nullable=True)

    failed_attempts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    avoid_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)

    # 0.0 until reuse data can calibrate it; never the provider's own estimate.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    generalizable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Normalised (kind, domain, title, problem) fingerprint, used for deterministic
    # duplicate detection. Indexed but deliberately **not** unique: the key is a
    # normalised string, and over-aggressive normalisation must never be able to
    # reject a legitimate experience (D-035).
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_experiences_dedup_key", "dedup_key"),
        Index("ix_experiences_kind", "kind"),
        Index("ix_experiences_status", "status"),
    )


class ExperienceSourceRow(Base):
    """``experience_sources`` table -- the runs that support an experience.

    A separate table rather than a ``source_run_id`` column (design document #18,
    round-5 brief section 13): one experience is normally confirmed by several runs,
    and turning "a single lucky run" into "knowledge that held up three times" is
    the whole point of the store. The composite primary key makes a duplicate link
    impossible, which is what backs the idempotency guarantee of
    ``ExperienceService.distill_run``.
    """

    __tablename__ = "experience_sources"

    experience_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("experiences.id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_experience_sources_run_id", "run_id"),)


class RetrievalSessionRow(Base):
    """``retrieval_sessions`` table -- one retrieval event (Milestone 7).

    A row here says "a search happened". Whether it found anything is a separate
    column, and a session with ``result_count = 0`` is kept rather than dropped:
    "we searched and found nothing" is the fact that tells an operator the knowledge
    base has a gap, and it is invisible if only successful searches are stored
    (round-7 brief, section 7).

    ``run_id`` is nullable and ``ON DELETE SET NULL`` rather than ``CASCADE``. A
    retrieval may precede the run it informs (section 24), and if the run is later
    deleted the retrieval still happened -- deleting the record of it would be
    rewriting history to match a cleanup. The row then honestly reads "unattributed",
    and the effectiveness report counts it as such.

    ``query_text`` holds the **sanitised** query, never a raw prompt (section 6);
    ``query_fingerprint`` is what groups repeated questions, so grouping survives
    redaction.
    """

    __tablename__ = "retrieval_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    run_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)

    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)

    knowledge_projection_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Recorded so a later reader can explain *why* this result was the one returned
    #: (section 59): policy and formatter versions change independently of the data.
    retrieval_policy_version: Mapped[str] = mapped_column(Text, nullable=False)

    retrieval_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Reserved for the holdout experiment of section 32. No behaviour today.
    experiment_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignment: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_retrieval_sessions_run_id", "run_id"),
        Index("ix_retrieval_sessions_created_at", "created_at"),
        Index("ix_retrieval_sessions_query_fingerprint", "query_fingerprint"),
    )


class ExperienceUsageRow(Base):
    """``experience_usage`` table -- one experience per retrieval session (Milestone 7).

    The composite UNIQUE constraint is the schema's most important statement: the
    same experience cannot appear twice in one retrieval result. A duplicate would
    not be "counted twice", it would mean the retriever returned one record in two
    slots, and the constraint turns that into a loud failure at the write instead of
    a quietly inflated statistic (section 49).

    Exposure and judgement columns are nullable/defaulted in opposite directions on
    purpose: ``retrieved_at`` is set the moment the row is created (the row only
    exists because it was retrieved), while everything downstream of it -- injection,
    signal, utility -- starts empty. Absence is the default state, and it means
    "unknown", never "no" (section 10).

    No outcome column. See :class:`~aer.runtime.models.ExperienceUsage` for why.
    """

    __tablename__ = "experience_usage"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    retrieval_session_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("retrieval_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    experience_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("experiences.id", ondelete="CASCADE"),
        nullable=False,
    )

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_score: Mapped[float] = mapped_column(Float, nullable=False)

    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    injected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    injection_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    injection_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    formatter_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    usage_signal: Mapped[str] = mapped_column(Text, nullable=False)
    usage_signal_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_signal_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    utility_label: Mapped[str] = mapped_column(Text, nullable=False)
    utility_label_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utility_label_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "retrieval_session_id",
            "experience_id",
            name="uq_experience_usage_session_experience",
        ),
        Index("ix_experience_usage_experience_id", "experience_id"),
        Index("ix_experience_usage_retrieval_session_id", "retrieval_session_id"),
        Index("ix_experience_usage_injected_at", "injected_at"),
        Index("ix_experience_usage_usage_signal", "usage_signal"),
    )


class AdapterSessionRow(Base):
    """``adapter_sessions`` table -- external Agent session to AER run (Milestone 8).

    Persisted rather than held in memory because the whole point of the table is to
    survive the Agent process restarting: a hook that reconnects must find the run it
    was already reporting into, not open a second trace for the same work (round-8
    brief, sections 20-21 and 51).

    The uniqueness key is ``(provider, external_session_id)``. A session id belongs
    to the vendor's namespace, so the vendor -- not the adapter implementation -- is
    what makes it unique; two adapters aimed at the same provider then cannot each
    claim the same external session.

    ``aer_run_id`` cascades on delete: a mapping to a run that no longer exists has
    nothing left to resume. The external ids are never primary keys (section 17);
    AER mints its own.
    """

    __tablename__ = "adapter_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_session_id: Mapped[str] = mapped_column(Text, nullable=False)

    adapter_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    aer_run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_session_id",
            name="uq_adapter_sessions_external",
        ),
        Index("ix_adapter_sessions_aer_run_id", "aer_run_id"),
        Index("ix_adapter_sessions_adapter_name", "adapter_name"),
    )


class AdapterEventRow(Base):
    """``adapter_events`` table -- the idempotency ledger (Milestone 8).

    One row per **accepted** external event, keyed by ``(provider,
    external_event_id)``. The unique constraint is the mechanism behind "a retried
    hook does not produce a second AER event" (sections 18-19): a duplicate delivery
    finds the existing row and is reported as a duplicate instead of being applied.

    A separate table rather than a column on ``events``: the core event table carries
    the trace's history, and the trace must not grow a column whose meaning is "how a
    particular vendor numbered this" (section 19's warning, and section 53's rule that
    M8 does not rewrite M1-M4 event semantics). It also keeps the ledger's own
    metadata -- which adapter accepted the event, when the vendor says it happened --
    from looking like part of the run.

    ``external_sequence`` / ``external_timestamp`` are the vendor's own ordering,
    stored beside AER's arrival-order ``sequence`` rather than replacing it
    (section 23).
    """

    __tablename__ = "adapter_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str] = mapped_column(Text, nullable=False)

    adapter_name: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    aer_run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The single AER event this external event became, when there is one.
    #: ``ON DELETE SET NULL`` because pruning an event must not invalidate the
    #: ledger's memory that the external event was already handled.
    aer_event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Set after the ledger row is claimed, so a row that exists but was never
    #: finished applying is distinguishable from one that was. Claiming first is what
    #: makes duplicate delivery impossible; losing an event to a crash between claim
    #: and apply is the trade, and it is visible here rather than silent.
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)

    external_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_event_id",
            name="uq_adapter_events_external",
        ),
        Index("ix_adapter_events_aer_run_id", "aer_run_id"),
        Index("ix_adapter_events_aer_event_id", "aer_event_id"),
    )
