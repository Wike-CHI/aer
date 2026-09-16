"""Persistence for :class:`~aer.runtime.models.Experience` and its sources.

Two repositories, one aggregate. ``ExperienceRepository.create`` accepts an optional
``source_run_id`` so that "the experience exists" and "we know which run produced
it" are written in **one transaction**: an experience whose provenance failed to
land would be knowledge nobody can trace back, and a later re-run would create a
second experience for the same claim. Every other write touches a single table and
is atomic on its own.
"""

from __future__ import annotations

import builtins
from typing import Any

from sqlalchemy import Select, func, select

from aer.exceptions import RecordNotFoundError, StorageError
from aer.runtime.enums import ExperienceKind, ExperienceStatus
from aer.runtime.models import Experience, ExperienceSource
from aer.storage.converters import (
    decode_enum,
    dump_json,
    dump_json_object,
    load_json,
    load_json_object,
)
from aer.storage.database import Database
from aer.storage.models import ExperienceRow, ExperienceSourceRow

#: Stored value of the terminal status, used by the "excluded" filters.
_DEPRECATED = ExperienceStatus.DEPRECATED.value

# NOTE: ``builtins.list`` is spelled out in the annotations below because this class
# defines a method called ``list`` (agent.md #15 requires that name for the
# repository API). Inside the class body the bare name ``list`` resolves to that
# method, so type annotations have to be explicit about which one they mean.

# NOTE: ``builtins.list`` is spelled out in the annotations below because this class
# defines a method called ``list`` (agent.md #15 requires that name for the
# repository API). Inside the class body the bare name ``list`` resolves to that
# method, so type annotations have to be explicit about which one they mean.


class ExperienceRepository:
    """CRUD access to the ``experiences`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        experience: Experience,
        *,
        source_run_id: str | None = None,
    ) -> Experience:
        """Insert ``experience``, optionally together with its first source link.

        Args:
            experience: The experience to store.
            source_run_id: The run that produced it. Written in the same
                transaction, so an experience can never exist without knowing where
                it came from.

        Raises:
            StorageError: the insert failed, including when the run does not exist.
        """
        with self._database.session(f"create experience {experience.id}") as session:
            session.add(_to_row(experience))
            if source_run_id is not None:
                session.add(
                    ExperienceSourceRow(
                        experience_id=experience.id,
                        run_id=source_run_id,
                        created_at=experience.created_at,
                    )
                )
            session.flush()
        return experience

    def get(self, experience_id: str) -> Experience | None:
        """Load an experience by id, or ``None`` when it does not exist."""
        experience: Experience | None
        with self._database.session(f"load experience {experience_id}") as session:
            row = session.get(ExperienceRow, experience_id)
            experience = None if row is None else _to_domain(row)
        return experience

    def update(self, experience: Experience) -> Experience:
        """Overwrite the stored row with ``experience``.

        Used for status transitions and metadata updates -- never for rewriting the
        claim itself, which would silently invalidate the dedup key.

        Raises:
            RecordNotFoundError: the experience was never persisted.
        """
        with self._database.session(f"update experience {experience.id}") as session:
            row = session.get(ExperienceRow, experience.id)
            if row is None:
                raise RecordNotFoundError(f"Experience not found: {experience.id}")
            _apply(row, experience)
            session.flush()
        return experience

    def list(
        self,
        *,
        kind: ExperienceKind | None = None,
        domain: str | None = None,
        status: ExperienceStatus | None = None,
        include_deprecated: bool = False,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> builtins.list[Experience]:
        """List stored experiences, newest first by default."""
        experiences: builtins.list[Experience]
        with self._database.session("list experiences") as session:
            statement: Select[Any] = _apply_filters(
                select(ExperienceRow),
                kind=kind,
                domain=domain,
                status=status,
                include_deprecated=include_deprecated,
            )
            order = (
                ExperienceRow.created_at.desc() if newest_first else ExperienceRow.created_at.asc()
            )
            statement = statement.order_by(order, ExperienceRow.id.asc())
            statement = statement.offset(offset).limit(limit)
            experiences = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return experiences

    def count(
        self,
        *,
        kind: ExperienceKind | None = None,
        domain: str | None = None,
        status: ExperienceStatus | None = None,
        include_deprecated: bool = False,
    ) -> int:
        """Number of stored experiences matching the same filters as :meth:`list`."""
        total: int
        with self._database.session("count experiences") as session:
            statement = _apply_filters(
                select(func.count()).select_from(ExperienceRow),
                kind=kind,
                domain=domain,
                status=status,
                include_deprecated=include_deprecated,
            )
            total = int(session.execute(statement).scalar_one())
        return total

    def find_by_dedup_key(
        self,
        dedup_key: str,
        *,
        include_deprecated: bool = False,
    ) -> builtins.list[Experience]:
        """Every experience whose normalised fingerprint equals ``dedup_key``.

        Deprecated experiences are excluded by default: withdrawn knowledge must not
        silently absorb new evidence. A caller that wants to inspect history can ask
        for them explicitly.
        """
        experiences: builtins.list[Experience]
        with self._database.session("find experiences by dedup key") as session:
            statement = select(ExperienceRow).where(ExperienceRow.dedup_key == dedup_key)
            if not include_deprecated:
                statement = statement.where(ExperienceRow.status != _DEPRECATED)
            statement = statement.order_by(ExperienceRow.created_at.asc(), ExperienceRow.id.asc())
            experiences = [_to_domain(row) for row in session.execute(statement).scalars().all()]
        return experiences


class ExperienceSourceRepository:
    """Read/write access to the ``experience_sources`` link table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, source: ExperienceSource) -> ExperienceSource:
        """Link ``source.run_id`` to ``source.experience_id``.

        Raises:
            StorageError: the link already exists, or either side does not exist.
                A duplicate is reported rather than ignored: callers are expected to
                have checked :meth:`get_experiences_for_run` first, so a collision
                means the idempotency assumption is wrong somewhere and should be
                seen.
        """
        with self._database.session(
            f"link experience {source.experience_id} to run {source.run_id}"
        ) as session:
            session.add(
                ExperienceSourceRow(
                    experience_id=source.experience_id,
                    run_id=source.run_id,
                    created_at=source.created_at,
                )
            )
            session.flush()
        return source

    def exists(self, experience_id: str, run_id: str) -> bool:
        """Whether this exact link is already recorded."""
        found: bool
        with self._database.session("check experience source") as session:
            found = session.get(ExperienceSourceRow, (experience_id, run_id)) is not None
        return found

    def get_runs(self, experience_id: str) -> list[str]:
        """Run ids supporting ``experience_id``, oldest link first."""
        run_ids: list[str]
        with self._database.session(f"load sources for experience {experience_id}") as session:
            statement = (
                select(ExperienceSourceRow.run_id)
                .where(ExperienceSourceRow.experience_id == experience_id)
                .order_by(ExperienceSourceRow.created_at.asc(), ExperienceSourceRow.run_id.asc())
            )
            run_ids = list(session.execute(statement).scalars().all())
        return run_ids

    def list_for_experience(self, experience_id: str) -> list[ExperienceSource]:
        """Every source link of ``experience_id``, oldest first."""
        sources: list[ExperienceSource]
        with self._database.session(f"load sources for experience {experience_id}") as session:
            statement = (
                select(ExperienceSourceRow)
                .where(ExperienceSourceRow.experience_id == experience_id)
                .order_by(ExperienceSourceRow.created_at.asc(), ExperienceSourceRow.run_id.asc())
            )
            sources = [_source_to_domain(row) for row in session.execute(statement).scalars().all()]
        return sources

    def get_experiences_for_run(self, run_id: str) -> list[str]:
        """Experience ids that this run supports.

        The primary idempotency probe: a run already linked to an experience has
        already been distilled, so a second ``distill_run`` for it returns the
        existing knowledge instead of producing a duplicate (round-5 brief, section 42).
        """
        experience_ids: list[str]
        with self._database.session(f"load experiences for run {run_id}") as session:
            statement = (
                select(ExperienceSourceRow.experience_id)
                .where(ExperienceSourceRow.run_id == run_id)
                .order_by(ExperienceSourceRow.created_at.asc())
            )
            experience_ids = list(session.execute(statement).scalars().all())
        return experience_ids

    def count_for_experience(self, experience_id: str) -> int:
        """How many runs support this experience."""
        total: int
        with self._database.session(f"count sources for experience {experience_id}") as session:
            statement = (
                select(func.count())
                .select_from(ExperienceSourceRow)
                .where(ExperienceSourceRow.experience_id == experience_id)
            )
            total = int(session.execute(statement).scalar_one())
        return total

    def remove(self, experience_id: str, run_id: str) -> None:
        """Delete one link.

        Only meaningful for correcting provenance; normal merging never removes a
        source.

        Raises:
            RecordNotFoundError: the link does not exist.
        """
        with self._database.session(
            f"unlink experience {experience_id} from run {run_id}"
        ) as session:
            row = session.get(ExperienceSourceRow, (experience_id, run_id))
            if row is None:
                raise RecordNotFoundError(
                    f"No source link between experience {experience_id} and run {run_id}"
                )
            session.delete(row)


# ---------------------------------------------------------------------------
# ORM <-> domain conversion (storage-layer detail)
# ---------------------------------------------------------------------------


def _apply_filters(
    statement: Select[Any],
    *,
    kind: ExperienceKind | None,
    domain: str | None,
    status: ExperienceStatus | None,
    include_deprecated: bool,
) -> Select[Any]:
    """Apply the filters shared by :meth:`ExperienceRepository.list` and ``count``.

    ``status`` and ``include_deprecated`` interact: asking for a specific status --
    including ``DEPRECATED`` -- wins over the default exclusion.
    """
    if kind is not None:
        statement = statement.where(ExperienceRow.kind == ExperienceKind(kind).value)
    if domain is not None:
        statement = statement.where(ExperienceRow.domain == domain)
    if status is not None:
        statement = statement.where(ExperienceRow.status == ExperienceStatus(status).value)
    elif not include_deprecated:
        statement = statement.where(ExperienceRow.status != _DEPRECATED)
    return statement


def _load_str_tuple(raw: str | None, *, context: str) -> tuple[str, ...]:
    """Decode a JSON list column into a tuple of strings.

    A missing column reads as an empty tuple (the domain model's default); anything
    that is not a list is refused, because it means the row was written by something
    that does not agree with this schema.
    """
    value = load_json(raw)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StorageError(f"{context} holds {type(value).__name__}, expected a list")
    return tuple(str(item) for item in value)


def _to_row(experience: Experience) -> ExperienceRow:
    row = ExperienceRow(id=experience.id)
    _apply(row, experience)
    return row


def _apply(row: ExperienceRow, experience: Experience) -> None:
    row.kind = experience.kind.value
    row.domain = experience.domain
    row.title = experience.title
    row.problem = experience.problem
    row.symptoms_json = dump_json(list(experience.symptoms))
    row.root_cause = experience.root_cause
    row.solution = experience.solution
    row.failed_attempts_json = dump_json(list(experience.failed_attempts))
    row.workflow_json = dump_json(list(experience.recommended_workflow))
    row.avoid_json = dump_json(list(experience.avoid))
    row.status = experience.status.value
    row.confidence = experience.confidence
    row.generalizable = experience.generalizable
    row.outcome_verified = experience.outcome_verified
    row.dedup_key = experience.dedup_key
    row.created_at = experience.created_at
    row.updated_at = experience.updated_at
    row.metadata_json = dump_json_object(experience.metadata)


def _to_domain(row: ExperienceRow) -> Experience:
    context = f"experience {row.id}"
    return Experience(
        id=row.id,
        kind=decode_enum(ExperienceKind, row.kind, context=context),
        domain=row.domain,
        title=row.title,
        problem=row.problem,
        dedup_key=row.dedup_key,
        symptoms=_load_str_tuple(row.symptoms_json, context=f"{context}.symptoms_json"),
        root_cause=row.root_cause,
        solution=row.solution,
        failed_attempts=_load_str_tuple(
            row.failed_attempts_json, context=f"{context}.failed_attempts_json"
        ),
        recommended_workflow=_load_str_tuple(row.workflow_json, context=f"{context}.workflow_json"),
        avoid=_load_str_tuple(row.avoid_json, context=f"{context}.avoid_json"),
        status=decode_enum(ExperienceStatus, row.status, context=context),
        confidence=row.confidence,
        generalizable=row.generalizable,
        outcome_verified=row.outcome_verified,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=load_json_object(row.metadata_json),
    )


def _source_to_domain(row: ExperienceSourceRow) -> ExperienceSource:
    return ExperienceSource(
        experience_id=row.experience_id,
        run_id=row.run_id,
        created_at=row.created_at,
    )
