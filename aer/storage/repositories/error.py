"""Persistence for :class:`~aer.runtime.models.ErrorRecord`."""

from __future__ import annotations

from sqlalchemy import func, select

from aer.exceptions import RecordNotFoundError
from aer.runtime.models import ErrorRecord
from aer.storage.converters import dump_json_object, load_json_object
from aer.storage.database import Database
from aer.storage.models import ErrorRow


class ErrorRepository:
    """CRUD access to the ``errors`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, error: ErrorRecord) -> ErrorRecord:
        """Insert ``error``.

        Returns the input model unchanged -- an ErrorRecord has no
        server-generated field, so there is nothing to hydrate back.
        """
        with self._database.session(f"create error {error.id}") as session:
            session.add(_to_row(error))
            session.flush()
        return error

    def get(self, error_id: str) -> ErrorRecord | None:
        """Load an error by id, or ``None`` when it does not exist."""
        error: ErrorRecord | None
        with self._database.session(f"load error {error_id}") as session:
            row = session.get(ErrorRow, error_id)
            error = None if row is None else _to_domain(row)
        return error

    def get_by_run(
        self,
        run_id: str,
        *,
        resolved: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ErrorRecord]:
        """Return a run's errors ordered by creation time.

        Args:
            run_id: The run to scope to.
            resolved: ``None`` for all errors, otherwise filter on the flag.
        """
        errors: list[ErrorRecord]
        with self._database.session(f"load errors for run {run_id}") as session:
            statement = select(ErrorRow).where(ErrorRow.run_id == run_id)
            if resolved is not None:
                statement = statement.where(ErrorRow.resolved.is_(resolved))

            statement = statement.order_by(ErrorRow.created_at.asc(), ErrorRow.id.asc()).offset(
                offset
            )
            if limit is not None:
                statement = statement.limit(limit)

            errors = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return errors

    def mark_resolved(self, error_id: str, *, resolved: bool = True) -> ErrorRecord:
        """Explicitly set the resolution flag on a single error.

        Only ever called from a deliberate link: a successful recovery that named
        this error. Nothing resolves errors as a side effect of a task later
        succeeding, and nothing bulk-resolves history (round-3 brief, section 18).

        Raises:
            RecordNotFoundError: no error with that id exists.
        """
        with self._database.session(f"mark error {error_id} resolved") as session:
            row = session.get(ErrorRow, error_id)
            if row is None:
                raise RecordNotFoundError(f"Error not found: {error_id}")
            row.resolved = resolved
            session.flush()
            return _to_domain(row)

    def count_by_run(self, run_id: str, *, resolved: bool | None = None) -> int:
        """Number of errors recorded for a run."""
        total: int
        with self._database.session(f"count errors for run {run_id}") as session:
            statement = select(func.count()).select_from(ErrorRow).where(ErrorRow.run_id == run_id)
            if resolved is not None:
                statement = statement.where(ErrorRow.resolved.is_(resolved))
            total = int(session.execute(statement).scalar_one())
        return total


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _to_row(error: ErrorRecord) -> ErrorRow:
    row = ErrorRow(id=error.id)
    _apply(row, error)
    return row


def _apply(row: ErrorRow, error: ErrorRecord) -> None:
    row.run_id = error.run_id
    row.event_id = error.event_id
    row.error_type = error.error_type
    row.error_message = error.error_message
    row.stack_trace = error.stack_trace
    row.recoverable = error.recoverable
    row.resolved = error.resolved
    row.created_at = error.created_at
    row.metadata_json = dump_json_object(error.metadata)


def _to_domain(row: ErrorRow) -> ErrorRecord:
    return ErrorRecord(
        id=row.id,
        run_id=row.run_id,
        event_id=row.event_id,
        error_type=row.error_type,
        error_message=row.error_message,
        stack_trace=row.stack_trace,
        recoverable=row.recoverable,
        resolved=row.resolved,
        created_at=row.created_at,
        metadata=load_json_object(row.metadata_json),
    )
