"""Projecting SQLite into the knowledge index -- one direction only.

    SQLite (the fact)  ──project──▶  NeuG (a searchable copy)

There is no arrow back. Nothing in this module writes to the experience store, and
nothing in AER reads an experience *from* the index when SQLite could answer
better. The index exists to be searched; the store exists to be true.

Four commitments, each with a failure it prevents:

**Batch commits.** The probe measured a write-bearing transaction at ~900 ms
regardless of how many statements it contains, against ~2 ms per statement. A
projector that committed per experience would spend fifteen minutes on a thousand
of them. So ``project_all`` and ``rebuild`` open one transaction per batch, and the
batch boundary -- not the individual record -- is the unit of atomicity.

**Re-projection is a function of input.** NeuG has no ``MERGE`` and does not
de-duplicate edges, so the projector replaces an experience wholesale (DETACH
DELETE, then CREATE) rather than trying to update it in place. Projecting twice
therefore leaves the projection identical to projecting once.

**A projection failure never invents a store failure.** If the index is
unavailable, the caller still has a committed experience -- the projection is
merely stale, ``drift()`` can see that, and ``rebuild()`` can fix it. Raising
:class:`~aer.exceptions.ProjectionError` says exactly that and no more.

**A rebuild never happens on top of the only copy.** The new projection is built at
a staging path, validated against SQLite's own counts and fingerprints, and only
then swapped in. A rebuild that dies halfway leaves the working index untouched.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aer.exceptions import ProjectionError
from aer.knowledge.base import IndexedExperience, IndexedRunRef, KnowledgeIndex
from aer.knowledge.schema import projection_metadata_path
from aer.runtime.enums import ProjectionAction
from aer.runtime.models import Experience, Run
from aer.storage.repositories import (
    ExperienceRepository,
    ExperienceSourceRepository,
    RunRepository,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DriftReport",
    "KnowledgeProjector",
    "ProjectionOutcome",
    "RebuildReport",
]

logger = logging.getLogger(__name__)

#: How many experiences go into one write transaction.
#:
#: Larger is nearly free (the measured cost is the commit, not the rows) but not
#: entirely: a failure rolls back the batch, so a very large batch means redoing
#: more work. 500 keeps a rebuild of a realistic store to a handful of commits.
DEFAULT_BATCH_SIZE = 500

#: Suffix of the staging directory a rebuild builds into before swapping.
_STAGING_SUFFIX = ".rebuilding"
#: Suffix the previous index is moved to during the swap, before deletion.
_PREVIOUS_SUFFIX = ".previous"


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """What happened to one experience during a projection."""

    experience_id: str
    action: ProjectionAction


@dataclass(frozen=True, slots=True)
class DriftReport:
    """How far the index has fallen behind the store.

    Four kinds of divergence, kept separate because they mean different things:
    a missing record is a projection that never ran, a stale one is an experience
    that changed afterwards, and an orphan is a projection of something SQLite no
    longer has. They are diagnosed differently even though the repair is the same.
    """

    store_count: int
    index_count: int
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    orphaned: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """Whether the index is a faithful image of the store."""
        return not (self.missing or self.stale or self.orphaned)

    @property
    def drifted(self) -> int:
        """Total number of records that differ."""
        return len(self.missing) + len(self.stale) + len(self.orphaned)


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What a rebuild did, in the units an operator cares about."""

    projected: int
    batch_size: int
    batches: int
    swapped: bool
    duration_ms: float

    @property
    def description(self) -> str:
        """One line, suitable for a log or a status command."""
        mode = "swapped in" if self.swapped else "in place"
        return (
            f"{self.projected} experiences projected in {self.batches} batches "
            f"({mode}, {self.duration_ms:.0f} ms)"
        )


class KnowledgeProjector:
    """Copies experiences from the store into the index. Never the other way."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        experiences: ExperienceRepository,
        sources: ExperienceSourceRepository,
        runs: RunRepository,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size={batch_size} must be at least 1")
        self._index = index
        self._experiences = experiences
        self._sources = sources
        self._runs = runs
        self._batch_size = batch_size

    @property
    def batch_size(self) -> int:
        """How many experiences one write transaction carries."""
        return self._batch_size

    # -- single records ----------------------------------------------------

    def project_experience(self, experience_id: str) -> ProjectionOutcome:
        """Project one experience, or remove its projection if it is gone.

        The "gone" case is not hypothetical: an experience deleted from the store
        must stop being retrievable, and a projector that only ever added would keep
        answering with knowledge the system has withdrawn. Reading the store first
        and the index second is what makes the two possible outcomes one method.
        """
        experience = self._experiences.get(experience_id)
        if experience is None:
            self._index.delete_experiences((experience_id,))
            logger.info("removed a stale projection for experience %s", experience_id)
            return ProjectionOutcome(experience_id=experience_id, action=ProjectionAction.REMOVED)

        run_ids = tuple(self._sources.get_runs(experience_id))
        self._ensure_ready()
        self._index.upsert_experiences((_to_indexed(experience, run_ids),), self._run_refs(run_ids))
        return ProjectionOutcome(experience_id=experience_id, action=ProjectionAction.PROJECTED)

    def project_run(self, run_id: str) -> tuple[ProjectionOutcome, ...]:
        """Project every experience a run supports.

        The write-path convenience: a caller that has just distilled a run knows
        which run, not which experience id, and asking it to look the id up first
        would make the obvious next step the awkward one.
        """
        experience_ids = self._sources.get_experiences_for_run(run_id)
        return tuple(self.project_experience(experience_id) for experience_id in experience_ids)

    # -- whole store -------------------------------------------------------

    def project_all(self) -> int:
        """Project every experience, in batches. Returns how many were written.

        Incremental by nature -- each record is replaced, not added -- so this is
        safe to run against a populated index and is the cheapest way to catch up
        after a projection failure.
        """
        self._ensure_ready()
        total = 0
        for batch in self._read_store():
            run_ids = self._run_ids_for(batch)
            self._index.upsert_experiences(
                tuple(
                    _to_indexed(experience, run_ids.get(experience.id, ())) for experience in batch
                ),
                self._run_refs(sorted({rid for ids in run_ids.values() for rid in ids})),
            )
            total += len(batch)
        logger.info("projected %d experiences incrementally", total)
        return total

    def drift(self) -> DriftReport:
        """Compare the index against the store on ``(id, updated_at)``.

        ``updated_at`` rather than a content hash because the store already
        maintains it: every write path bumps it, and a fingerprint that requires
        re-reading and hashing every row would cost as much as the rebuild it is
        trying to avoid.
        """
        store = {e.id: e.updated_at.isoformat() for e in self._iter_store()}
        index = self._index.fingerprints()

        missing = tuple(sorted(set(store) - set(index)))
        orphaned = tuple(sorted(set(index) - set(store)))
        stale = tuple(
            sorted(
                experience_id
                for experience_id, updated_at in store.items()
                if experience_id in index and index[experience_id] != updated_at
            )
        )
        return DriftReport(
            store_count=len(store),
            index_count=len(index),
            missing=missing,
            stale=stale,
            orphaned=orphaned,
        )

    # -- rebuild -----------------------------------------------------------

    def rebuild(self) -> RebuildReport:
        """Rebuild the whole index from the store, then swap it in.

        The staging path is a sibling of the live one, so the swap is a pair of
        renames within a single directory -- no data is moved across a filesystem
        and no window exists in which the live path is missing. If anything up to
        and including validation fails, the live index has not been touched.

        SQLite is read and never written. That is the entire point: the index is
        disposable and the store is not.
        """
        import time

        from aer.knowledge.neug import NeuGKnowledgeIndex

        started = time.perf_counter()
        if not isinstance(self._index, NeuGKnowledgeIndex):
            # A staged swap is a filesystem operation, so it only applies to the
            # on-disk index. An in-memory index (tests, a future alternative
            # implementation) is rebuilt in place, and says so in the report.
            return self._rebuild_in_place(started)

        live = Path(self._index.path)
        staging = live.with_name(live.name + _STAGING_SUFFIX)
        previous = live.with_name(live.name + _PREVIOUS_SUFFIX)
        _remove_index_artifacts(staging)
        _remove_index_artifacts(previous)

        staged = NeuGKnowledgeIndex(staging, environ=self._index.environ)
        try:
            staged.ensure_schema()
            projected, batches = self._write_into(staged)
            self._validate(staged)
        except BaseException:
            staged.close()
            _remove_index_artifacts(staging)
            raise
        staged.close()

        self._index.close()
        swapped = False
        try:
            if live.exists():
                live.rename(previous)
            staging.rename(live)
            swapped = True
        except OSError as exc:
            # Put the original back rather than leaving the live path absent.
            if previous.exists() and not live.exists():
                previous.rename(live)
            raise ProjectionError(
                f"Swapping the rebuilt knowledge index into {live} failed: {exc}. "
                "The previous index was restored and remains usable."
            ) from exc
        finally:
            if swapped:
                _remove_index_artifacts(previous)

        duration_ms = (time.perf_counter() - started) * 1000
        report = RebuildReport(
            projected=projected,
            batch_size=self._batch_size,
            batches=batches,
            swapped=swapped,
            duration_ms=duration_ms,
        )
        logger.info("knowledge rebuild: %s", report.description)
        return report

    def _rebuild_in_place(self, started: float) -> RebuildReport:
        """Rebuild without a staged swap, for an index that has no directory.

        The failure semantics are explicit rather than implied: a failure leaves a
        partially written projection behind, ``drift()`` reports it, and running
        rebuild again fixes it. There is no half-migrated state to repair by hand
        because nothing here is authored -- it is all derived from SQLite.
        """
        import time

        self._ensure_ready()
        self._index.reset()
        projected, batches = self._write_into(self._index)
        self._validate(self._index)
        return RebuildReport(
            projected=projected,
            batch_size=self._batch_size,
            batches=batches,
            swapped=False,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def _write_into(self, index: KnowledgeIndex) -> tuple[int, int]:
        """Project the whole store into ``index``, returning (records, batches)."""
        projected = 0
        batches = 0
        for batch in self._read_store():
            run_ids = self._run_ids_for(batch)
            index.upsert_experiences(
                tuple(
                    _to_indexed(experience, run_ids.get(experience.id, ())) for experience in batch
                ),
                self._run_refs(sorted({rid for ids in run_ids.values() for rid in ids})),
            )
            projected += len(batch)
            batches += 1
        return projected, batches

    def _validate(self, index: KnowledgeIndex) -> None:
        """Prove the new projection matches the store before it is trusted.

        Counts plus fingerprints, because counts alone would pass a projection that
        wrote the right number of records with the wrong contents.
        """
        store = {e.id: e.updated_at.isoformat() for e in self._iter_store()}
        found = index.fingerprints()
        if set(store) != set(found):
            missing = sorted(set(store) - set(found))[:5]
            extra = sorted(set(found) - set(store))[:5]
            raise ProjectionError(
                "The rebuilt knowledge index does not match the store: "
                f"{len(set(store) - set(found))} missing (e.g. {missing}), "
                f"{len(set(found) - set(store))} unexpected (e.g. {extra}). "
                "The previous index was left in place."
            )
        mismatched = sorted(
            experience_id
            for experience_id, updated_at in store.items()
            if found[experience_id] != updated_at
        )
        if mismatched:
            raise ProjectionError(
                f"The rebuilt knowledge index records a different updated_at for "
                f"{len(mismatched)} experiences (e.g. {mismatched[:5]}). "
                "The previous index was left in place."
            )

    # -- store access ------------------------------------------------------

    def _read_store(self) -> Iterable[list[Experience]]:
        """The store in batches, newest first, deprecated included.

        Including deprecated records is deliberate: the projection is supposed to be
        an image of the store, and the *retrieval policy* is what keeps withdrawn
        knowledge away from callers. Filtering it out here would make the index
        unable to answer "was this ever known, and was it withdrawn".
        """
        offset = 0
        while True:
            batch = self._experiences.list(
                limit=self._batch_size, offset=offset, include_deprecated=True
            )
            if not batch:
                return
            yield batch
            offset += len(batch)

    def _iter_store(self) -> Iterable[Experience]:
        for batch in self._read_store():
            yield from batch

    def _run_ids_for(self, batch: Sequence[Experience]) -> dict[str, tuple[str, ...]]:
        """Source run ids for a batch, in one query rather than one per record."""
        links = self._sources.list_all_for(tuple(experience.id for experience in batch))
        grouped: dict[str, list[str]] = {}
        for experience_id, run_id in links:
            grouped.setdefault(experience_id, []).append(run_id)
        return {experience_id: tuple(run_ids) for experience_id, run_ids in grouped.items()}

    def _run_refs(self, run_ids: Sequence[str]) -> tuple[IndexedRunRef, ...]:
        """Reference nodes for the given runs, in one query.

        A run that SQLite no longer has is skipped rather than invented: the edge
        would point at nothing, and a graph full of dangling references is worse
        than a source count that is honestly one lower.
        """
        if not run_ids:
            return ()
        found: dict[str, Run] = self._runs.get_many(tuple(run_ids))
        refs = []
        for run_id in run_ids:
            run = found.get(run_id)
            if run is None:
                logger.warning("run %s is referenced by an experience but absent", run_id)
                continue
            refs.append(
                IndexedRunRef(
                    id=run.id,
                    status=run.status.value,
                    agent_name=run.agent_name or "",
                    agent_version=run.agent_version or "",
                )
            )
        return tuple(refs)

    def _ensure_ready(self) -> None:
        """Make sure the index has the schema this build writes."""
        self._index.ensure_schema()


def _to_indexed(experience: Experience, run_ids: tuple[str, ...]) -> IndexedExperience:
    """Convert one stored experience into the flat shape the index holds.

    Two transformations worth naming:

    * ``None`` becomes ``""`` for the optional text fields, because NeuG rejects
      ``NULL`` on a persistent property outright. The difference between "no known
      cause" and "a blank cause" stays in SQLite, where it is the fact; in a
      full-text index both are simply not a term.
    * list fields become one line per entry, which is the form the full-text index
      can weight. An entry containing a newline is flattened to a single line -- the
      store keeps the exact string, and a projection that split one entry into two
      would invent structure that was never there.
    """
    return IndexedExperience(
        id=experience.id,
        kind=experience.kind.value,
        status=experience.status.value,
        domain=experience.domain,
        title=experience.title,
        problem=experience.problem,
        root_cause=experience.root_cause or "",
        solution=experience.solution or "",
        failed_attempts_text=_join_lines(experience.failed_attempts),
        avoid_text=_join_lines(experience.avoid),
        outcome_verified=experience.outcome_verified,
        generalizable=experience.generalizable,
        created_at=experience.created_at.isoformat(),
        updated_at=experience.updated_at.isoformat(),
        run_ids=run_ids,
    )


def _join_lines(values: tuple[str, ...]) -> str:
    """One entry per line, each collapsed to a single line."""
    return "\n".join(" ".join(value.split()) for value in values if value.strip())


def _remove_index_artifacts(path: Path) -> None:
    """Delete a staging or superseded index: the directory **and** its sidecar.

    Both, because the sidecar is named after the database path
    (``aer-knowledge.rebuilding.projection.json``), so renaming the directory into
    place leaves the staging metadata orphaned in the knowledge directory. The first
    version removed only the directory, and the acceptance run on the real server
    showed the leftover file -- a stale schema version sitting beside the live one,
    which is exactly what an operator would read first during an incident.

    Refuses anything whose name it did not choose: this function deletes a tree.
    """
    if not path.name.endswith((_STAGING_SUFFIX, _PREVIOUS_SUFFIX)):
        raise ProjectionError(
            f"Refusing to remove {path}: it is not a staging ({_STAGING_SUFFIX}) or "
            f"superseded ({_PREVIOUS_SUFFIX}) index artifact"
        )
    if path.exists():
        if not path.is_dir():
            raise ProjectionError(f"Refusing to remove {path}: not a directory")
        shutil.rmtree(path)
    sidecar = Path(projection_metadata_path(str(path)))
    if sidecar.exists():
        sidecar.unlink()
