"""SQLAlchemy ORM tables for the AER SQLite store (Milestone 2 Task 2.2, M3).

Realised tables:

* ``runs``          -- one row per agent task execution
* ``events``        -- one row per standardised runtime event
* ``errors``        -- structured failures (Milestone 3)
* ``recoveries``    -- recovery attempts, optionally bound to an error (Milestone 3)
* ``verifications`` -- independent verdicts on a run's outcome (Milestone 4)
* ``experiences``   -- distilled, reusable knowledge (Milestone 5)
* ``experience_sources`` -- which runs support an experience (Milestone 5)

Still pending from the agreed MVP schema: ``artifacts``, ``experience_usage``,
``workflows`` and ``dataset_items``. Their SQL is already agreed in the design
document, so adding them later is additive work; creating empty tables now would
only add untested surface area.

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
