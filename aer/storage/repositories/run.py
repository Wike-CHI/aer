"""Persistence for :class:`~aer.runtime.models.Run`."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from aer.exceptions import RecordNotFoundError
from aer.runtime.enums import RunStatus
from aer.runtime.models import Run
from aer.storage.converters import decode_enum, dump_json_object, load_json_object
from aer.storage.database import Database
from aer.storage.models import RunRow

DEFAULT_PAGE_SIZE = 100


class RunRepository:
    """CRUD access to the ``runs`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, run: Run) -> Run:
        """Insert ``run``.

        Returns the input model unchanged -- a Run has no server-generated field,
        so there is nothing to hydrate back.
        """
        with self._database.session(f"create run {run.id}") as session:
            session.add(_to_row(run))
            session.flush()
        return run

    def get(self, run_id: str) -> Run | None:
        """Load a run by id, or ``None`` when it does not exist."""
        run: Run | None
        with self._database.session(f"load run {run_id}") as session:
            row = session.get(RunRow, run_id)
            run = None if row is None else _to_domain(row)
        return run

    def update(self, run: Run) -> Run:
        """Overwrite the stored run with ``run``.

        Raises:
            RecordNotFoundError: the run was never persisted (or was deleted).
        """
        with self._database.session(f"update run {run.id}") as session:
            row = session.get(RunRow, run.id)
            if row is None:
                raise RecordNotFoundError(f"Run not found: {run.id}")
            _apply(row, run)
            session.flush()
        return run

    def list(
        self,
        *,
        status: RunStatus | None = None,
        task_type: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[Run]:
        """List runs, newest first by default."""
        runs: list[Run]
        with self._database.session("list runs") as session:
            statement = select(RunRow)
            if status is not None:
                statement = statement.where(RunRow.status == status.value)
            if task_type is not None:
                statement = statement.where(RunRow.task_type == task_type)

            order = RunRow.started_at.desc() if newest_first else RunRow.started_at.asc()
            statement = statement.order_by(order, RunRow.id.asc()).offset(offset).limit(limit)

            runs = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return runs

    def count(self, *, status: RunStatus | None = None, task_type: str | None = None) -> int:
        """Number of runs matching the given filters."""
        total: int
        with self._database.session("count runs") as session:
            statement = select(func.count()).select_from(RunRow)
            if status is not None:
                statement = statement.where(RunRow.status == status.value)
            if task_type is not None:
                statement = statement.where(RunRow.task_type == task_type)
            total = int(session.execute(statement).scalar_one())
        return total

    def get_many(self, run_ids: Sequence[str]) -> dict[str, Run]:
        """Load several runs by id in one query, keyed by id.

        Exists for the knowledge projector, which needs a reference node per source
        run and would otherwise issue one query per experience -- the N+1 that
        section 83 of the round-6 brief rules out. Ids that do not exist are simply
        absent from the result: a caller that needs to know has ``len()``.
        """
        if not run_ids:
            return {}
        runs: dict[str, Run]
        with self._database.session("load runs by id") as session:
            statement = select(RunRow).where(RunRow.id.in_(list(run_ids)))
            runs = {row.id: _to_domain(row) for row in session.execute(statement).scalars().all()}
        return runs


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _to_row(run: Run) -> RunRow:
    row = RunRow(id=run.id)
    _apply(row, run)
    return row


def _apply(row: RunRow, run: Run) -> None:
    row.task_type = run.task_type
    row.task_description = run.task_description
    row.agent_name = run.agent_name
    row.agent_version = run.agent_version
    row.model_provider = run.model_provider
    row.model_name = run.model_name
    row.status = run.status.value
    row.started_at = run.started_at
    row.ended_at = run.ended_at
    row.final_score = run.final_score
    row.metadata_json = dump_json_object(run.metadata)


def _to_domain(row: RunRow) -> Run:
    return Run(
        id=row.id,
        task_type=row.task_type,
        task_description=row.task_description,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        model_provider=row.model_provider,
        model_name=row.model_name,
        status=decode_enum(RunStatus, row.status, context=f"Run {row.id} status"),
        started_at=row.started_at,
        ended_at=row.ended_at,
        final_score=row.final_score,
        metadata=load_json_object(row.metadata_json),
    )
