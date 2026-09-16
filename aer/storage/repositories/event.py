"""Persistence for :class:`~aer.runtime.models.Event`.

Event ordering is the core invariant of this repository: inside a run, ``sequence``
must be dense and strictly increasing (TASKS.md Task 3.3). Ordering is therefore
derived from ``sequence`` only -- never from ``created_at``, which two events of
the same run may share.
"""

from __future__ import annotations

from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aer.runtime.enums import EventType
from aer.runtime.models import Event
from aer.storage.converters import (
    decode_enum,
    dump_json,
    dump_json_object,
    load_json,
    load_json_object,
)
from aer.storage.database import Database
from aer.storage.models import EventRow


class EventRepository:
    """Create and read ``events`` rows."""

    def __init__(self, database: Database) -> None:
        self._database = database
        # Serialises "read max(sequence) then insert" for this process. Concurrent
        # writers from other processes are out of scope for the embedded milestone;
        # the UNIQUE(run_id, sequence) constraint keeps even that case from
        # producing a corrupted trace.
        self._sequence_lock = Lock()

    def create(self, event: Event) -> Event:
        """Persist ``event`` and return it hydrated with ``id`` and ``sequence``.

        ``event.sequence`` is honoured when provided; when it is ``None`` the next
        free sequence for the run is allocated inside the same transaction.
        """
        action = f"create event for run {event.run_id}"
        with self._sequence_lock, self._database.session(action) as session:
            sequence = event.sequence
            if sequence is None:
                sequence = _next_sequence(session, event.run_id)

            row = EventRow(
                run_id=event.run_id,
                sequence=sequence,
                event_type=event.event_type.value,
                input_json=dump_json(event.input),
                output_json=dump_json(event.output),
                created_at=event.created_at,
                duration_ms=event.duration_ms,
                metadata_json=dump_json_object(event.metadata),
            )
            session.add(row)
            session.flush()  # assigns row.id, triggers FK + unique checks
            # Returning from inside `with` still runs the session's commit
            # (contextmanager __exit__) before the caller sees the value.
            return _to_domain(row)

    def get(self, event_id: int) -> Event | None:
        """Load a single event by its database id."""
        event: Event | None
        with self._database.session(f"load event {event_id}") as session:
            row = session.get(EventRow, event_id)
            event = None if row is None else _to_domain(row)
        return event

    def get_by_run(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Event]:
        """Return a run's events ordered by ``sequence`` ascending."""
        events: list[Event]
        with self._database.session(f"load events for run {run_id}") as session:
            statement = (
                select(EventRow)
                .where(EventRow.run_id == run_id)
                .order_by(EventRow.sequence.asc())
                .offset(offset)
            )
            if limit is not None:
                statement = statement.limit(limit)
            events = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return events

    def get_next_sequence(self, run_id: str) -> int:
        """Return the sequence number the next event of ``run_id`` will receive.

        Exposed for callers that need to know the ordering in advance (for example
        to reference an event before it is written). It is advisory: the value is
        recomputed inside :meth:`create`, so it can never be stale.
        """
        with self._database.session(f"read next sequence for run {run_id}") as session:
            return _next_sequence(session, run_id)

    def count_by_run(self, run_id: str) -> int:
        """Number of events recorded for a run."""
        total: int
        with self._database.session(f"count events for run {run_id}") as session:
            statement = select(func.count()).select_from(EventRow).where(EventRow.run_id == run_id)
            total = int(session.execute(statement).scalar_one())
        return total


def _next_sequence(session: Session, run_id: str) -> int:
    """Compute ``max(sequence) + 1`` for a run, or 1 when it has no events yet."""
    statement = select(func.max(EventRow.sequence)).where(EventRow.run_id == run_id)
    current = session.execute(statement).scalar()
    return 1 if current is None else int(current) + 1


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _to_domain(row: EventRow) -> Event:
    return Event(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        event_type=decode_enum(EventType, row.event_type, context=f"Event {row.id} type"),
        input=load_json(row.input_json),
        output=load_json(row.output_json),
        created_at=row.created_at,
        duration_ms=row.duration_ms,
        metadata=load_json_object(row.metadata_json),
    )
