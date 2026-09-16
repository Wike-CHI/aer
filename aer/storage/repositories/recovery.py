"""Persistence for :class:`~aer.runtime.models.RecoveryRecord`.

A recovery row is created when ``RECOVERY_START`` is recorded and updated when
``RECOVERY_RESULT`` lands. Mirroring :class:`~aer.runtime.run.RunContext` this way
means an interrupted process leaves a visible row with ``success IS NULL`` rather
than no row at all -- an event without its record would be a silently inconsistent
trace.
"""

from __future__ import annotations

from sqlalchemy import func, select

from aer.exceptions import RecordNotFoundError
from aer.runtime.models import RecoveryRecord
from aer.storage.converters import dump_json, dump_json_object, load_json, load_json_object
from aer.storage.database import Database
from aer.storage.models import RecoveryRow


class RecoveryRepository:
    """CRUD access to the ``recoveries`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, recovery: RecoveryRecord) -> RecoveryRecord:
        """Insert ``recovery``, typically while it is still open."""
        with self._database.session(f"create recovery {recovery.id}") as session:
            session.add(_to_row(recovery))
            session.flush()
        return recovery

    def get(self, recovery_id: str) -> RecoveryRecord | None:
        """Load a recovery by id, or ``None`` when it does not exist."""
        recovery: RecoveryRecord | None
        with self._database.session(f"load recovery {recovery_id}") as session:
            row = session.get(RecoveryRow, recovery_id)
            recovery = None if row is None else _to_domain(row)
        return recovery

    def update(self, recovery: RecoveryRecord) -> RecoveryRecord:
        """Overwrite the stored row with ``recovery``.

        Raises:
            RecordNotFoundError: the recovery was never persisted.
        """
        with self._database.session(f"update recovery {recovery.id}") as session:
            row = session.get(RecoveryRow, recovery.id)
            if row is None:
                raise RecordNotFoundError(f"Recovery not found: {recovery.id}")
            _apply(row, recovery)
            session.flush()
        return recovery

    def get_by_run(
        self, run_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[RecoveryRecord]:
        """Return a run's recovery attempts ordered by start time."""
        recoveries: list[RecoveryRecord]
        with self._database.session(f"load recoveries for run {run_id}") as session:
            statement = (
                select(RecoveryRow)
                .where(RecoveryRow.run_id == run_id)
                .order_by(RecoveryRow.started_at.asc(), RecoveryRow.id.asc())
                .offset(offset)
            )
            if limit is not None:
                statement = statement.limit(limit)

            recoveries = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return recoveries

    def get_by_error(self, error_id: str) -> list[RecoveryRecord]:
        """Return every recovery attempt that targeted ``error_id``."""
        recoveries: list[RecoveryRecord]
        with self._database.session(f"load recoveries for error {error_id}") as session:
            statement = (
                select(RecoveryRow)
                .where(RecoveryRow.error_id == error_id)
                .order_by(RecoveryRow.started_at.asc(), RecoveryRow.id.asc())
            )
            recoveries = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return recoveries

    def count_by_run(self, run_id: str) -> int:
        """Number of recovery attempts recorded for a run."""
        total: int
        with self._database.session(f"count recoveries for run {run_id}") as session:
            statement = (
                select(func.count()).select_from(RecoveryRow).where(RecoveryRow.run_id == run_id)
            )
            total = int(session.execute(statement).scalar_one())
        return total


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _to_row(recovery: RecoveryRecord) -> RecoveryRow:
    row = RecoveryRow(id=recovery.id)
    _apply(row, recovery)
    return row


def _apply(row: RecoveryRow, recovery: RecoveryRecord) -> None:
    row.run_id = recovery.run_id
    row.error_id = recovery.error_id
    row.start_event_id = recovery.start_event_id
    row.result_event_id = recovery.result_event_id
    row.reason = recovery.reason
    row.success = recovery.success
    row.duration_ms = recovery.duration_ms
    row.outcome_json = dump_json(recovery.outcome)
    row.started_at = recovery.started_at
    row.ended_at = recovery.ended_at
    row.metadata_json = dump_json_object(recovery.metadata)


def _to_domain(row: RecoveryRow) -> RecoveryRecord:
    return RecoveryRecord(
        id=row.id,
        run_id=row.run_id,
        error_id=row.error_id,
        start_event_id=row.start_event_id,
        result_event_id=row.result_event_id,
        reason=row.reason,
        success=row.success,
        duration_ms=row.duration_ms,
        outcome=load_json(row.outcome_json),
        started_at=row.started_at,
        ended_at=row.ended_at,
        metadata=load_json_object(row.metadata_json),
    )
