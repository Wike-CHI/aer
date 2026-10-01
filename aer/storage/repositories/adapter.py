"""Persistence for the Agent-adapter protocol (Milestone 8).

Two tables, two jobs, and both of them exist because an adapter is a *foreign*
process talking to AER over a protocol rather than a piece of AER:

* :class:`AdapterSessionRepository` owns ``external session -> AER run``. It is a
  table rather than a dictionary because an Agent process restarts far more often
  than a task finishes, and a hook that reconnects has to find the run it was
  already reporting into (round-8 brief, sections 20-21 and 51);
* :class:`AdapterEventRepository` owns the idempotency ledger. Real hooks retry, real
  webhooks are delivered twice, and the answer to a second delivery must be "already
  handled", not "a second event" (sections 18-19).

Neither table is part of the trace. The events an adapter produces land in
``events`` through the ordinary runtime path; these rows describe the *integration*
-- which external session, which adapter, which vendor numbering -- and they are
deliberately kept beside the trace instead of inside it (section 53).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from sqlalchemy import func, select

from aer.exceptions import RecordNotFoundError
from aer.runtime.enums import EventType
from aer.runtime.models import AdapterEventRecord, AdapterSession
from aer.storage.converters import decode_enum, dump_json_object, load_json_object
from aer.storage.database import Database
from aer.storage.models import AdapterEventRow, AdapterSessionRow

__all__ = ["AdapterEventRepository", "AdapterSessionRepository"]


class AdapterSessionRepository:
    """Read/write access to the ``adapter_sessions`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, session: AdapterSession) -> AdapterSession:
        """Insert a new external-session mapping.

        Raises:
            StorageError: the insert failed -- including when this provider already
                has a mapping for this external session id, which is the
                database-level half of "one external session, one current run".
        """
        with self._database.session(
            f"create adapter session {session.provider}/{session.external_session_id}"
        ) as db:
            db.add(_session_to_row(session))
            db.flush()
        return session

    def get(self, session_id: str) -> AdapterSession | None:
        """Load a mapping by its AER id, or ``None`` when it does not exist."""
        session: AdapterSession | None
        with self._database.session(f"load adapter session {session_id}") as db:
            row = db.get(AdapterSessionRow, session_id)
            session = None if row is None else _session_to_domain(row)
        return session

    def find(self, provider: str, external_session_id: str) -> AdapterSession | None:
        """Load the mapping for one external session, or ``None``.

        The lookup every reconnect performs. Keyed on the vendor's identifiers
        because that is what a reconnecting Agent can actually tell us.
        """
        session: AdapterSession | None
        with self._database.session(f"load adapter session {provider}/{external_session_id}") as db:
            statement = select(AdapterSessionRow).where(
                AdapterSessionRow.provider == provider,
                AdapterSessionRow.external_session_id == external_session_id,
            )
            row = db.execute(statement).scalars().first()
            session = None if row is None else _session_to_domain(row)
        return session

    def update(self, session: AdapterSession) -> AdapterSession:
        """Overwrite the stored mapping with ``session``.

        Used when a caller explicitly reopens a finished session onto a new run: the
        previous run ids travel in ``metadata``, so this is an append-only change
        rather than a forgotten history.

        Raises:
            RecordNotFoundError: the mapping does not exist.
        """
        with self._database.session(f"update adapter session {session.id}") as db:
            row = db.get(AdapterSessionRow, session.id)
            if row is None:
                raise RecordNotFoundError(f"Adapter session not found: {session.id}")
            _apply_session(row, session)
            db.flush()
        return session

    def list(
        self,
        *,
        provider: str | None = None,
        adapter_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> builtins.list[AdapterSession]:
        """List external-session mappings, newest first by default."""
        sessions: builtins.list[AdapterSession]
        with self._database.session("list adapter sessions") as db:
            statement = select(AdapterSessionRow)
            if provider is not None:
                statement = statement.where(AdapterSessionRow.provider == provider)
            if adapter_name is not None:
                statement = statement.where(AdapterSessionRow.adapter_name == adapter_name)
            order = (
                AdapterSessionRow.created_at.desc()
                if newest_first
                else AdapterSessionRow.created_at.asc()
            )
            statement = statement.order_by(order, AdapterSessionRow.id.asc())
            statement = statement.offset(offset).limit(limit)
            sessions = [_session_to_domain(row) for row in db.execute(statement).scalars().all()]
        return sessions

    def count(
        self,
        *,
        provider: str | None = None,
        adapter_name: str | None = None,
    ) -> int:
        """Number of mappings matching the same filters as :meth:`list`."""
        total: int
        with self._database.session("count adapter sessions") as db:
            statement = select(func.count()).select_from(AdapterSessionRow)
            if provider is not None:
                statement = statement.where(AdapterSessionRow.provider == provider)
            if adapter_name is not None:
                statement = statement.where(AdapterSessionRow.adapter_name == adapter_name)
            total = int(db.execute(statement).scalar_one())
        return total


class AdapterEventRepository:
    """Read/write access to the ``adapter_events`` idempotency ledger."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, record: AdapterEventRecord) -> AdapterEventRecord:
        """Insert one ledger entry for an external event that was applied.

        Raises:
            StorageError: the insert failed -- including when this provider has
                already accepted this external event id. That collision is reported
                rather than ignored: the caller is supposed to have checked
                :meth:`find` first, so reaching the constraint means two deliveries
                raced, and a silently swallowed error there would be exactly the
                double-application this table exists to prevent.
        """
        with self._database.session(
            f"record adapter event {record.provider}/{record.external_event_id}"
        ) as db:
            db.add(_event_to_row(record))
            db.flush()
        return record

    def mark_applied(
        self, record_id: str, *, aer_event_id: int | None = None
    ) -> AdapterEventRecord:
        """Mark a claimed external event as fully applied.

        Raises:
            RecordNotFoundError: the claim does not exist. That means the caller
                applied an event it never claimed, which is the one ordering the
                ledger exists to prevent.
        """
        with self._database.session(f"mark adapter event {record_id} applied") as db:
            row = db.get(AdapterEventRow, record_id)
            if row is None:
                raise RecordNotFoundError(f"Adapter event not found: {record_id}")
            row.applied = True
            if aer_event_id is not None:
                row.aer_event_id = aer_event_id
            db.flush()
            return _event_to_domain(row)

    def get(self, record_id: str) -> AdapterEventRecord | None:
        """Load a ledger entry by its AER id, or ``None``."""
        record: AdapterEventRecord | None
        with self._database.session(f"load adapter event {record_id}") as db:
            row = db.get(AdapterEventRow, record_id)
            record = None if row is None else _event_to_domain(row)
        return record

    def find(self, provider: str, external_event_id: str) -> AdapterEventRecord | None:
        """The ledger entry for one external event, or ``None``.

        The idempotency probe. It is deliberately a single indexed lookup on the same
        columns as the unique constraint, so "have we seen this already?" and "may I
        insert it?" cannot disagree about what identity means.
        """
        record: AdapterEventRecord | None
        with self._database.session(f"load adapter event {provider}/{external_event_id}") as db:
            statement = select(AdapterEventRow).where(
                AdapterEventRow.provider == provider,
                AdapterEventRow.external_event_id == external_event_id,
            )
            row = db.execute(statement).scalars().first()
            record = None if row is None else _event_to_domain(row)
        return record

    def list(
        self,
        *,
        aer_run_id: str | None = None,
        provider: str | None = None,
        adapter_name: str | None = None,
        applied: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> builtins.list[AdapterEventRecord]:
        """List accepted external events, newest first by default."""
        records: builtins.list[AdapterEventRecord]
        with self._database.session("list adapter events") as db:
            statement = select(AdapterEventRow)
            if aer_run_id is not None:
                statement = statement.where(AdapterEventRow.aer_run_id == aer_run_id)
            if provider is not None:
                statement = statement.where(AdapterEventRow.provider == provider)
            if adapter_name is not None:
                statement = statement.where(AdapterEventRow.adapter_name == adapter_name)
            if applied is not None:
                statement = statement.where(AdapterEventRow.applied.is_(applied))
            order = (
                AdapterEventRow.created_at.desc()
                if newest_first
                else AdapterEventRow.created_at.asc()
            )
            statement = statement.order_by(order, AdapterEventRow.id.asc())
            statement = statement.offset(offset).limit(limit)
            records = [_event_to_domain(row) for row in db.execute(statement).scalars().all()]
        return records

    def count(
        self,
        *,
        aer_run_id: str | None = None,
        provider: str | None = None,
        applied: bool | None = None,
    ) -> int:
        """Number of ledger entries matching the given filters.

        ``applied=False`` is the interesting filter operationally: it counts external
        events that were claimed and never finished being applied, which is the one
        visible trace of an ingest that died in the middle (see the module docstring on
        why the ledger is at-most-once).
        """
        total: int
        with self._database.session("count adapter events") as db:
            statement = select(func.count()).select_from(AdapterEventRow)
            if aer_run_id is not None:
                statement = statement.where(AdapterEventRow.aer_run_id == aer_run_id)
            if provider is not None:
                statement = statement.where(AdapterEventRow.provider == provider)
            if applied is not None:
                statement = statement.where(AdapterEventRow.applied.is_(applied))
            total = int(db.execute(statement).scalar_one())
        return total

    def count_for_runs(self, run_ids: Sequence[str]) -> dict[str, int]:
        """Ledger entries per AER run, in one query, for the runs given.

        Exists so a debugging view over many sessions does not become one query per
        session -- the same N+1 rule the effectiveness report follows (round-7 brief,
        section 56), applied to the integration surface.
        """
        if not run_ids:
            return {}
        counts: dict[str, int]
        with self._database.session("count adapter events by run") as db:
            statement = (
                select(AdapterEventRow.aer_run_id, func.count())
                .where(AdapterEventRow.aer_run_id.in_(list(run_ids)))
                .group_by(AdapterEventRow.aer_run_id)
            )
            counts = {row[0]: int(row[1]) for row in db.execute(statement).all()}
        return counts


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _session_to_row(session: AdapterSession) -> AdapterSessionRow:
    row = AdapterSessionRow(id=session.id)
    _apply_session(row, session)
    return row


def _apply_session(row: AdapterSessionRow, session: AdapterSession) -> None:
    row.provider = session.provider
    row.external_session_id = session.external_session_id
    row.adapter_name = session.adapter_name
    row.external_run_id = session.external_run_id
    row.aer_run_id = session.aer_run_id
    row.protocol_version = session.protocol_version
    row.created_at = session.created_at
    row.updated_at = session.updated_at
    row.metadata_json = dump_json_object(session.metadata)


def _session_to_domain(row: AdapterSessionRow) -> AdapterSession:
    return AdapterSession(
        id=row.id,
        provider=row.provider,
        external_session_id=row.external_session_id,
        adapter_name=row.adapter_name,
        external_run_id=row.external_run_id,
        aer_run_id=row.aer_run_id,
        protocol_version=row.protocol_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=load_json_object(row.metadata_json),
    )


def _event_to_row(record: AdapterEventRecord) -> AdapterEventRow:
    row = AdapterEventRow(id=record.id)
    row.provider = record.provider
    row.external_event_id = record.external_event_id
    row.adapter_name = record.adapter_name
    row.event_type = EventType(record.event_type).value
    row.aer_run_id = record.aer_run_id
    row.aer_event_id = record.aer_event_id
    row.applied = record.applied
    row.external_sequence = record.external_sequence
    row.external_timestamp = record.external_timestamp
    row.created_at = record.created_at
    row.metadata_json = dump_json_object(record.metadata)
    return row


def _event_to_domain(row: AdapterEventRow) -> AdapterEventRecord:
    return AdapterEventRecord(
        id=row.id,
        provider=row.provider,
        external_event_id=row.external_event_id,
        adapter_name=row.adapter_name,
        event_type=decode_enum(EventType, row.event_type, context=f"adapter event {row.id}"),
        aer_run_id=row.aer_run_id,
        applied=row.applied,
        aer_event_id=row.aer_event_id,
        external_sequence=row.external_sequence,
        external_timestamp=row.external_timestamp,
        created_at=row.created_at,
        metadata=load_json_object(row.metadata_json),
    )
