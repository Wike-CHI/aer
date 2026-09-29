"""Persistence for retrieval sessions and experience usage (Milestone 7).

Two repositories, one aggregate, for the same reason
:mod:`aer.storage.repositories.experience` has two: a session and the usage rows
beneath it are written in **one transaction**. A session that recorded five results
but stored no rows would report popularity it cannot substantiate, and the
``UNIQUE(retrieval_session_id, experience_id)`` constraint would never be able to
catch a duplicate because there would be nothing to collide with.

Everything here is a behaviour fact -- what was searched, what was returned, what
was injected, what anyone said about it. None of it is derivable from the runs or
the experiences, which is why it is stored rather than computed, and none of it is
projected into the knowledge index (round-7 brief, section 45).

The read side is shaped for the effectiveness report, not for convenience: the
batch readers exist so that a report over N experiences issues a fixed number of
queries instead of one per experience per metric (section 56).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Select, func, select

from aer.exceptions import RecordNotFoundError, StorageError
from aer.runtime.enums import (
    RetrievalMode,
    SessionAssignment,
    UsageRole,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)
from aer.runtime.models import ExperienceUsage, RetrievalSession
from aer.storage.converters import (
    decode_enum,
    dump_json_object,
    load_json_object,
)
from aer.storage.database import Database
from aer.storage.models import ExperienceUsageRow, RetrievalSessionRow

__all__ = ["ExperienceUsageRepository", "RetrievalSessionRepository"]


class RetrievalSessionRepository:
    """CRUD access to the ``retrieval_sessions`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        session: RetrievalSession,
        *,
        usages: Sequence[ExperienceUsage] = (),
    ) -> RetrievalSession:
        """Insert ``session`` and its usage rows in one transaction.

        Args:
            session: The retrieval to record.
            usages: One row per experience the result carried, in result order. An
                empty sequence is a normal case, not an error: a zero-result
                retrieval still records that it happened (round-7 brief, section 7).

        Raises:
            StorageError: the write failed -- including when a usage row names a
                different session, names an experience that does not exist, or
                repeats an experience already in this session. **Nothing is
                persisted** in that case, so a caller never observes a session whose
                rows are half-written.
        """
        mismatched = [
            usage.experience_id for usage in usages if usage.retrieval_session_id != session.id
        ]
        if mismatched:
            raise StorageError(
                f"Usage rows for experiences {sorted(mismatched)} name a different "
                f"retrieval session than {session.id}"
            )

        with self._database.session(f"create retrieval session {session.id}") as db:
            db.add(_session_to_row(session))
            # Flush the parent before the children so the failure mode is "the
            # session is missing" rather than an ordering surprise in the unit of
            # work. Still one transaction: both land or neither does.
            db.flush()
            for usage in usages:
                db.add(_usage_to_row(usage))
            db.flush()
        return session

    def get(self, session_id: str) -> RetrievalSession | None:
        """Load a session by id, or ``None`` when it does not exist."""
        session: RetrievalSession | None
        with self._database.session(f"load retrieval session {session_id}") as db:
            row = db.get(RetrievalSessionRow, session_id)
            session = None if row is None else _session_to_domain(row)
        return session

    def get_many(self, session_ids: Sequence[str]) -> dict[str, RetrievalSession]:
        """Load several sessions by id in one query, keyed by id.

        For the effectiveness report, which needs the target run of every session
        beneath a set of usage rows and must not ask once per row (section 56).
        """
        if not session_ids:
            return {}
        sessions: dict[str, RetrievalSession]
        with self._database.session("load retrieval sessions by id") as db:
            statement = select(RetrievalSessionRow).where(
                RetrievalSessionRow.id.in_(list(session_ids))
            )
            sessions = {
                row.id: _session_to_domain(row) for row in db.execute(statement).scalars().all()
            }
        return sessions

    def update(self, session: RetrievalSession) -> RetrievalSession:
        """Overwrite the stored session with ``session``.

        Used for attaching a late-arriving run id, and nothing else: a session is a
        record of an observation, so rewriting its query or its result count would
        be falsifying history.

        Raises:
            RecordNotFoundError: the session was never persisted.
        """
        with self._database.session(f"update retrieval session {session.id}") as db:
            row = db.get(RetrievalSessionRow, session.id)
            if row is None:
                raise RecordNotFoundError(f"Retrieval session not found: {session.id}")
            _apply_session(row, session)
            db.flush()
        return session

    def list(
        self,
        *,
        run_id: str | None = None,
        query_fingerprint: str | None = None,
        domain: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> builtins.list[RetrievalSession]:
        """List recorded sessions, newest first by default."""
        sessions: builtins.list[RetrievalSession]
        with self._database.session("list retrieval sessions") as db:
            statement = select(RetrievalSessionRow)
            if run_id is not None:
                statement = statement.where(RetrievalSessionRow.run_id == run_id)
            if query_fingerprint is not None:
                statement = statement.where(
                    RetrievalSessionRow.query_fingerprint == query_fingerprint
                )
            if domain is not None:
                statement = statement.where(RetrievalSessionRow.domain == domain)
            order = (
                RetrievalSessionRow.created_at.desc()
                if newest_first
                else RetrievalSessionRow.created_at.asc()
            )
            statement = statement.order_by(order, RetrievalSessionRow.id.asc())
            statement = statement.offset(offset).limit(limit)
            sessions = [_session_to_domain(row) for row in db.execute(statement).scalars().all()]
        return sessions

    def count(self, *, run_id: str | None = None, query_fingerprint: str | None = None) -> int:
        """Number of recorded sessions matching the same filters as :meth:`list`."""
        total: int
        with self._database.session("count retrieval sessions") as db:
            statement = select(func.count()).select_from(RetrievalSessionRow)
            if run_id is not None:
                statement = statement.where(RetrievalSessionRow.run_id == run_id)
            if query_fingerprint is not None:
                statement = statement.where(
                    RetrievalSessionRow.query_fingerprint == query_fingerprint
                )
            total = int(db.execute(statement).scalar_one())
        return total


class ExperienceUsageRepository:
    """CRUD access to the ``experience_usage`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- writes ------------------------------------------------------------

    def create(self, usage: ExperienceUsage) -> ExperienceUsage:
        """Insert one usage row.

        Raises:
            StorageError: the insert failed, including when the session, the
                experience or the pair already exists.
        """
        with self._database.session(f"create usage of experience {usage.experience_id}") as db:
            db.add(_usage_to_row(usage))
            db.flush()
        return usage

    def update(self, usage: ExperienceUsage) -> ExperienceUsage:
        """Overwrite the stored row with ``usage``.

        This is how an injection, a usage signal and a utility label are recorded:
        each is new information about an exposure that already happened, not a
        separate event.

        Raises:
            RecordNotFoundError: the row does not exist.
        """
        with self._database.session(f"update usage {usage.id}") as db:
            row = db.get(ExperienceUsageRow, usage.id)
            if row is None:
                raise RecordNotFoundError(f"Experience usage not found: {usage.id}")
            _apply_usage(row, usage)
            db.flush()
        return usage

    def update_many(self, usages: Sequence[ExperienceUsage]) -> builtins.list[ExperienceUsage]:
        """Overwrite several rows in one transaction.

        Injection writes every experience that entered one rendered context, and
        that is one event: a context is injected as a whole or not at all. Writing
        them one transaction at a time would leave a half-injected context behind on
        failure -- some experiences marked as having been in a prompt that the agent
        never saw.

        Raises:
            RecordNotFoundError: a row does not exist. Nothing is written.
        """
        if not usages:
            return []
        with self._database.session(f"update {len(usages)} usage rows") as db:
            for usage in usages:
                row = db.get(ExperienceUsageRow, usage.id)
                if row is None:
                    raise RecordNotFoundError(f"Experience usage not found: {usage.id}")
                _apply_usage(row, usage)
            db.flush()
        return list(usages)

    # -- reads -------------------------------------------------------------

    def get(self, usage_id: str) -> ExperienceUsage | None:
        """Load a usage row by id, or ``None`` when it does not exist."""
        usage: ExperienceUsage | None
        with self._database.session(f"load usage {usage_id}") as db:
            row = db.get(ExperienceUsageRow, usage_id)
            usage = None if row is None else _usage_to_domain(row)
        return usage

    def get_by_session_and_experience(
        self, session_id: str, experience_id: str
    ) -> ExperienceUsage | None:
        """The single row for one experience in one session, or ``None``.

        The lookup behind every injection and signal write: the composite unique key
        makes this a primary-key-shaped read rather than a scan.
        """
        usage: ExperienceUsage | None
        with self._database.session("load usage by session and experience") as db:
            statement = select(ExperienceUsageRow).where(
                ExperienceUsageRow.retrieval_session_id == session_id,
                ExperienceUsageRow.experience_id == experience_id,
            )
            row = db.execute(statement).scalars().first()
            usage = None if row is None else _usage_to_domain(row)
        return usage

    def get_by_session(self, session_id: str) -> builtins.list[ExperienceUsage]:
        """Every row of one session, in the order the agent saw them."""
        usages: builtins.list[ExperienceUsage]
        with self._database.session(f"load usage for session {session_id}") as db:
            statement = (
                select(ExperienceUsageRow)
                .where(ExperienceUsageRow.retrieval_session_id == session_id)
                .order_by(ExperienceUsageRow.rank.asc(), ExperienceUsageRow.id.asc())
            )
            usages = [_usage_to_domain(row) for row in db.execute(statement).scalars().all()]
        return usages

    def list_for_experiences(
        self,
        experience_ids: Sequence[str],
        *,
        injected_only: bool = False,
    ) -> builtins.list[ExperienceUsage]:
        """Every row for a set of experiences, in one query.

        The aggregate read behind the effectiveness report. Sections 55-56 are
        explicit that the report may not become an N+1, so the alternatives -- one
        query per experience for retrievals, another for injections -- are not
        available here even though they would be easier to read.
        """
        if not experience_ids:
            return []
        usages: builtins.list[ExperienceUsage]
        with self._database.session("load usage for experiences") as db:
            statement = select(ExperienceUsageRow).where(
                ExperienceUsageRow.experience_id.in_(list(experience_ids))
            )
            if injected_only:
                statement = statement.where(ExperienceUsageRow.injected_at.is_not(None))
            statement = statement.order_by(
                ExperienceUsageRow.experience_id.asc(),
                ExperienceUsageRow.retrieved_at.asc(),
                ExperienceUsageRow.id.asc(),
            )
            usages = [_usage_to_domain(row) for row in db.execute(statement).scalars().all()]
        return usages

    def list_for_experience(
        self,
        experience_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> builtins.list[ExperienceUsage]:
        """One experience's usage history, newest first by default."""
        usages: builtins.list[ExperienceUsage]
        with self._database.session(f"list usage for experience {experience_id}") as db:
            statement = select(ExperienceUsageRow).where(
                ExperienceUsageRow.experience_id == experience_id
            )
            order = (
                ExperienceUsageRow.retrieved_at.desc()
                if newest_first
                else ExperienceUsageRow.retrieved_at.asc()
            )
            statement = statement.order_by(order, ExperienceUsageRow.id.asc())
            statement = statement.offset(offset).limit(limit)
            usages = [_usage_to_domain(row) for row in db.execute(statement).scalars().all()]
        return usages

    def count(
        self,
        *,
        experience_id: str | None = None,
        session_id: str | None = None,
        injected_only: bool = False,
    ) -> int:
        """Number of usage rows matching the given filters."""
        total: int
        with self._database.session("count experience usage") as db:
            statement: Select[Any] = select(func.count()).select_from(ExperienceUsageRow)
            if experience_id is not None:
                statement = statement.where(ExperienceUsageRow.experience_id == experience_id)
            if session_id is not None:
                statement = statement.where(ExperienceUsageRow.retrieval_session_id == session_id)
            if injected_only:
                statement = statement.where(ExperienceUsageRow.injected_at.is_not(None))
            total = int(db.execute(statement).scalar_one())
        return total

    def injected_run_ids(self, experience_id: str) -> builtins.list[str]:
        """Distinct runs this experience was injected into, sorted.

        The one question the promotion policy asks about reuse -- "has it been used
        somewhere other than where it came from?" -- answered as a single join rather
        than by reading every usage row and every session in Python. A limit on the
        row scan would also have been a correctness bug here: an experience injected
        into ten thousand runs must not be judged on the first hundred.
        """
        run_ids: builtins.list[str]
        with self._database.session(f"load injected runs for experience {experience_id}") as db:
            statement = (
                select(RetrievalSessionRow.run_id)
                .join(
                    ExperienceUsageRow,
                    ExperienceUsageRow.retrieval_session_id == RetrievalSessionRow.id,
                )
                .where(
                    ExperienceUsageRow.experience_id == experience_id,
                    ExperienceUsageRow.injected_at.is_not(None),
                    RetrievalSessionRow.run_id.is_not(None),
                )
                .distinct()
                .order_by(RetrievalSessionRow.run_id.asc())
            )
            run_ids = [row for row in db.execute(statement).scalars().all() if row is not None]
        return run_ids

    def distinct_experience_ids(self) -> builtins.list[str]:
        """Ids of every experience with at least one usage row, sorted.

        What a whole-store report iterates over. Deriving the set from the usage
        table rather than from ``experiences`` keeps a report over a large store
        proportional to the behaviour that exists, not to the knowledge that could.
        """
        ids: builtins.list[str]
        with self._database.session("list experiences with usage") as db:
            statement = (
                select(ExperienceUsageRow.experience_id)
                .distinct()
                .order_by(ExperienceUsageRow.experience_id.asc())
            )
            ids = list(db.execute(statement).scalars().all())
        return ids


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _session_to_row(session: RetrievalSession) -> RetrievalSessionRow:
    row = RetrievalSessionRow(id=session.id)
    _apply_session(row, session)
    return row


def _apply_session(row: RetrievalSessionRow, session: RetrievalSession) -> None:
    row.run_id = session.run_id
    row.query_text = session.query_text
    row.query_fingerprint = session.query_fingerprint
    row.domain = session.domain
    row.mode = RetrievalMode(session.mode).value
    row.requested_limit = session.requested_limit
    row.result_count = session.result_count
    row.knowledge_projection_version = session.knowledge_projection_version
    row.retrieval_policy_version = session.retrieval_policy_version
    row.retrieval_duration_ms = session.retrieval_duration_ms
    row.experiment_id = session.experiment_id
    row.assignment = SessionAssignment(session.assignment).value
    row.created_at = session.created_at
    row.metadata_json = dump_json_object(session.metadata)


def _session_to_domain(row: RetrievalSessionRow) -> RetrievalSession:
    return RetrievalSession(
        id=row.id,
        run_id=row.run_id,
        query_text=row.query_text,
        query_fingerprint=row.query_fingerprint,
        domain=row.domain,
        mode=decode_enum(RetrievalMode, row.mode, context=f"retrieval session {row.id} mode"),
        requested_limit=row.requested_limit,
        result_count=row.result_count,
        knowledge_projection_version=row.knowledge_projection_version,
        retrieval_policy_version=row.retrieval_policy_version,
        retrieval_duration_ms=row.retrieval_duration_ms,
        experiment_id=row.experiment_id,
        assignment=decode_enum(
            SessionAssignment, row.assignment, context=f"retrieval session {row.id} assignment"
        ),
        created_at=row.created_at,
        metadata=load_json_object(row.metadata_json),
    )


def _usage_to_row(usage: ExperienceUsage) -> ExperienceUsageRow:
    row = ExperienceUsageRow(id=usage.id)
    _apply_usage(row, usage)
    return row


def _apply_usage(row: ExperienceUsageRow, usage: ExperienceUsage) -> None:
    row.retrieval_session_id = usage.retrieval_session_id
    row.experience_id = usage.experience_id
    row.rank = usage.rank
    row.role = UsageRole(usage.role).value
    row.retrieval_score = usage.retrieval_score
    row.retrieved_at = usage.retrieved_at
    row.injected_at = usage.injected_at
    row.injection_position = usage.injection_position
    row.injection_chars = usage.injection_chars
    row.context_fingerprint = usage.context_fingerprint
    row.formatter_version = usage.formatter_version
    row.usage_signal = UsageSignal(usage.usage_signal).value
    row.usage_signal_source = (
        None
        if usage.usage_signal_source is None
        else UsageSignalSource(usage.usage_signal_source).value
    )
    row.usage_signal_at = usage.usage_signal_at
    row.utility_label = UtilityLabel(usage.utility_label).value
    row.utility_label_source = (
        None
        if usage.utility_label_source is None
        else UtilitySource(usage.utility_label_source).value
    )
    row.utility_label_at = usage.utility_label_at
    row.created_at = usage.created_at
    row.updated_at = usage.updated_at
    row.metadata_json = dump_json_object(usage.metadata)


def _usage_to_domain(row: ExperienceUsageRow) -> ExperienceUsage:
    context = f"experience usage {row.id}"
    try:
        return ExperienceUsage(
            id=row.id,
            retrieval_session_id=row.retrieval_session_id,
            experience_id=row.experience_id,
            rank=row.rank,
            role=decode_enum(UsageRole, row.role, context=context),
            retrieval_score=row.retrieval_score,
            retrieved_at=row.retrieved_at,
            injected_at=row.injected_at,
            injection_position=row.injection_position,
            injection_chars=row.injection_chars,
            context_fingerprint=row.context_fingerprint,
            formatter_version=row.formatter_version,
            usage_signal=decode_enum(UsageSignal, row.usage_signal, context=context),
            usage_signal_source=(
                None
                if row.usage_signal_source is None
                else decode_enum(UsageSignalSource, row.usage_signal_source, context=context)
            ),
            usage_signal_at=row.usage_signal_at,
            utility_label=decode_enum(UtilityLabel, row.utility_label, context=context),
            utility_label_source=(
                None
                if row.utility_label_source is None
                else decode_enum(UtilitySource, row.utility_label_source, context=context)
            ),
            utility_label_at=row.utility_label_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=load_json_object(row.metadata_json),
        )
    except ValidationError as exc:
        # The domain model enforces invariants the schema cannot (a signal and its
        # source travel together; injection detail needs an injection). Only a
        # hand-edited row or one written by an incompatible build can violate them, and
        # both are storage problems -- reported as such rather than as a pydantic
        # error leaking out of a repository read.
        raise StorageError(
            f"{context} does not satisfy the usage invariants: {exc}. The row was not "
            "written by this version of AER; inspect it before trusting the table."
        ) from exc
