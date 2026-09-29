"""Persistence for :class:`~aer.runtime.models.VerificationRecord`.

Verdicts are written once and never updated: a verification is an *observation*
of the world at a point in time, so re-checking the same thing twice produces two
rows rather than a mutation. That is why there is no ``update`` here, unlike
:class:`~aer.storage.repositories.recovery.RecoveryRepository` (a recovery attempt
genuinely has an open state that gets completed).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, case, func, select

from aer.runtime.enums import VerifierType
from aer.runtime.models import VerificationRecord
from aer.storage.converters import decode_enum, dump_json_object, load_json_object
from aer.storage.database import Database
from aer.storage.models import VerificationRow
from aer.verification.summary import VerificationSummary


class VerificationRepository:
    """CRUD access to the ``verifications`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, verification: VerificationRecord) -> VerificationRecord:
        """Insert ``verification``.

        Returns the input model unchanged -- a verification has no
        server-generated field, so there is nothing to hydrate back.
        """
        with self._database.session(f"create verification {verification.id}") as session:
            session.add(_to_row(verification))
            session.flush()
        return verification

    def get(self, verification_id: str) -> VerificationRecord | None:
        """Load a verdict by id, or ``None`` when it does not exist."""
        verification: VerificationRecord | None
        with self._database.session(f"load verification {verification_id}") as session:
            row = session.get(VerificationRow, verification_id)
            verification = None if row is None else _to_domain(row)
        return verification

    def get_by_run(
        self,
        run_id: str,
        *,
        passed: bool | None = None,
        required: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[VerificationRecord]:
        """Return a run's verdicts in the order they were recorded.

        Args:
            run_id: The run to scope to.
            passed: ``None`` for all verdicts, otherwise filter on the outcome.
            required: ``None`` for all verdicts, otherwise required/optional only.
        """
        verifications: list[VerificationRecord]
        with self._database.session(f"load verifications for run {run_id}") as session:
            statement = select(VerificationRow).where(VerificationRow.run_id == run_id)
            if passed is not None:
                statement = statement.where(VerificationRow.passed.is_(passed))
            if required is not None:
                statement = statement.where(VerificationRow.required.is_(required))

            statement = statement.order_by(
                VerificationRow.created_at.asc(), VerificationRow.id.asc()
            ).offset(offset)
            if limit is not None:
                statement = statement.limit(limit)

            verifications = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return verifications

    def count_by_run(
        self,
        run_id: str,
        *,
        passed: bool | None = None,
        required: bool | None = None,
    ) -> int:
        """Number of verdicts recorded for a run."""
        total: int
        with self._database.session(f"count verifications for run {run_id}") as session:
            statement = (
                select(func.count())
                .select_from(VerificationRow)
                .where(VerificationRow.run_id == run_id)
            )
            if passed is not None:
                statement = statement.where(VerificationRow.passed.is_(passed))
            if required is not None:
                statement = statement.where(VerificationRow.required.is_(required))
            total = int(session.execute(statement).scalar_one())
        return total

    def summaries_by_run(self, run_ids: Sequence[str]) -> dict[str, VerificationSummary]:
        """Aggregate verdicts for several runs in one query, keyed by run id.

        Added for the effectiveness report. Asking :meth:`get_by_run` per run and
        aggregating in Python would read every verdict row of every run beneath a set
        of usage rows -- the N+1 that round-7 brief section 56 forbids -- and the
        aggregation would then exist in two places.

        **A run with no verdicts is absent from the result, not present with
        zeroes.** The absence is load-bearing: :class:`VerificationSummary` treats
        ``total == 0`` as "nobody checked", and a caller that reads a missing key as
        a summary would compute a pass rate out of nothing.
        """
        if not run_ids:
            return {}
        summaries: dict[str, VerificationSummary]
        with self._database.session("summarise verifications by run") as session:
            statement = (
                select(
                    VerificationRow.run_id.label("run_id"),
                    func.count().label("total"),
                    func.sum(case((VerificationRow.passed.is_(True), 1), else_=0)).label("passed"),
                    func.sum(case((VerificationRow.required.is_(True), 1), else_=0)).label(
                        "required_total"
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    VerificationRow.required.is_(True),
                                    VerificationRow.passed.is_(True),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("required_passed"),
                )
                .where(VerificationRow.run_id.in_(list(run_ids)))
                .group_by(VerificationRow.run_id)
            )
            rows = session.execute(statement).all()
            summaries = {
                row.run_id: VerificationSummary.from_counts(
                    row.run_id,
                    total=int(row.total),
                    passed=int(row.passed or 0),
                    required_total=int(row.required_total or 0),
                    required_passed=int(row.required_passed or 0),
                )
                for row in rows
            }
        return summaries


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _to_row(verification: VerificationRecord) -> VerificationRow:
    row = VerificationRow(id=verification.id)
    _apply(row, verification)
    return row


def _apply(row: VerificationRow, verification: VerificationRecord) -> None:
    row.run_id = verification.run_id
    row.event_id = verification.event_id
    row.verifier_type = verification.verifier_type.value
    row.verifier_name = verification.verifier_name
    row.passed = verification.passed
    row.required = verification.required
    row.score = verification.score
    row.message = verification.message
    row.result_json = dump_json_object(verification.result)
    row.created_at = verification.created_at
    row.metadata_json = dump_json_object(verification.metadata)


def _to_domain(row: VerificationRow) -> VerificationRecord:
    return VerificationRecord(
        id=row.id,
        run_id=row.run_id,
        event_id=row.event_id,
        verifier_type=decode_enum(
            VerifierType, row.verifier_type, context=f"verification {row.id}"
        ),
        verifier_name=row.verifier_name,
        passed=row.passed,
        required=row.required,
        score=row.score,
        message=row.message,
        result=load_json_object(row.result_json),
        created_at=row.created_at,
        metadata=load_json_object(row.metadata_json),
    )
