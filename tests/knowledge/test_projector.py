"""Projection semantics: one direction, batched, idempotent, repairable.

Everything here runs against the recording index, so the assertions are about what
AER asked the index to do rather than about what the engine did with it. The engine's
half -- that a second ``CREATE`` really does produce a second edge, which is *why*
the projector deletes first -- was established by the capability probe and is
re-checked against the real engine in ``test_neug_index.py``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aer import AER, ExperienceSource, ExperienceStatus
from aer.exceptions import ProjectionError
from aer.knowledge.projector import KnowledgeProjector, _remove_index_artifacts
from aer.knowledge.schema import projection_metadata_path
from aer.runtime.enums import ProjectionAction
from aer.storage.models import ExperienceRow, ExperienceSourceRow
from tests.knowledge.conftest import NOW, store_experience, store_run
from tests.knowledge.support import RecordingIndex, indexed_experience


class TestSingleProjection:
    def test_it_writes_the_experience_and_its_source(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        store_run(knowledge_runtime, "run-1")
        experience = store_experience(knowledge_runtime, source_run_id="run-1")

        outcome = projector.project_experience(experience.id)

        assert outcome.action is ProjectionAction.PROJECTED
        written, refs = knowledge_index.upserts[0]
        assert [record.id for record in written] == [experience.id]
        assert written[0].run_ids == ("run-1",)
        assert [ref.id for ref in refs] == ["run-1"]

    def test_the_projected_fields_match_the_store(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        record = knowledge_index.projected[experience.id]

        assert record.kind == "RECOVERY"
        assert record.status == "VERIFIED"
        assert record.domain == "wordpress"
        assert record.title == experience.title
        assert record.problem == experience.problem
        assert record.outcome_verified is True
        assert record.generalizable is True
        assert record.created_at == experience.created_at.isoformat()
        assert record.updated_at == experience.updated_at.isoformat()

    def test_a_deprecated_experience_is_still_projected(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        """The index is an image of the store; the *policy* keeps deprecated away.

        Filtering it out here would leave the index unable to answer whether
        something was once known and then withdrawn.
        """
        experience = store_experience(knowledge_runtime, status=ExperienceStatus.DEPRECATED)
        projector.project_experience(experience.id)
        assert knowledge_index.projected[experience.id].status == "DEPRECATED"

    def test_absent_optional_text_becomes_empty_not_null(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        """NeuG rejects NULL on a persistent property, so the projection maps it.

        The distinction between "no known cause" and "a blank cause" stays in
        SQLite, where it is a fact rather than a missing term.
        """
        experience = store_experience(knowledge_runtime, root_cause=None, solution=None)
        projector.project_experience(experience.id)
        record = knowledge_index.projected[experience.id]
        assert record.root_cause == ""
        assert record.solution == ""

    def test_list_fields_become_one_line_per_entry(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(
            knowledge_runtime,
            failed_attempts=("盲目重试", "刷新缓存"),
            avoid=("不要盲目重试",),
        )
        projector.project_experience(experience.id)
        record = knowledge_index.projected[experience.id]
        assert record.failed_attempts_text == "盲目重试\n刷新缓存"
        assert record.avoid_text == "不要盲目重试"

    def test_a_multiline_entry_is_flattened_to_one_line(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        """Otherwise reading the field back would invent two entries.

        The store keeps the exact string; the projection is a search-and-display
        form, and silently splitting one claim into two would be inventing structure
        that was never there.
        """
        experience = store_experience(knowledge_runtime, failed_attempts=("first\nsecond",))
        projector.project_experience(experience.id)
        assert knowledge_index.projected[experience.id].failed_attempts_text == "first second"


class TestIdempotency:
    def test_projecting_three_times_leaves_one_projection(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(knowledge_runtime)
        for _ in range(3):
            projector.project_experience(experience.id)
        assert len(knowledge_index.projected) == 1
        assert knowledge_index.written_count == 3

    def test_each_pass_writes_identical_content(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        projector.project_experience(experience.id)
        first, second = knowledge_index.upserts[0][0], knowledge_index.upserts[1][0]
        assert first == second

    def test_an_update_replaces_rather_than_accumulates(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        knowledge_runtime.experiences.update(
            experience.model_copy(update={"title": "改写后的标题"})
        )
        projector.project_experience(experience.id)

        assert len(knowledge_index.projected) == 1
        assert knowledge_index.projected[experience.id].title == "改写后的标题"

    def test_a_second_source_does_not_replace_the_first(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        store_run(knowledge_runtime, "run-1")
        store_run(knowledge_runtime, "run-2")
        experience = store_experience(knowledge_runtime, source_run_id="run-1")
        projector.project_experience(experience.id)
        knowledge_runtime.experience_sources.add(
            ExperienceSource(experience_id=experience.id, run_id="run-2")
        )
        projector.project_experience(experience.id)

        assert knowledge_index.projected[experience.id].run_ids == ("run-1", "run-2")
        assert knowledge_index.source_counts((experience.id,)) == {experience.id: 2}


class TestMultipleSources:
    def test_every_source_run_is_preserved(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        for run_id in ("run-001", "run-017", "run-083"):
            store_run(knowledge_runtime, run_id)
        experience = store_experience(knowledge_runtime, source_run_id="run-001")
        for run_id in ("run-017", "run-083"):
            knowledge_runtime.experience_sources.add(
                ExperienceSource(experience_id=experience.id, run_id=run_id)
            )

        projector.project_experience(experience.id)

        assert knowledge_index.projected[experience.id].run_ids == (
            "run-001",
            "run-017",
            "run-083",
        )
        assert {ref.id for ref in knowledge_index.upserts[0][1]} == {
            "run-001",
            "run-017",
            "run-083",
        }

    def test_a_missing_run_does_not_produce_a_dangling_reference(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        """A reference to a run the store no longer has would point at nothing."""
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        assert knowledge_index.run_refs == {}


class TestRemoval:
    def test_a_deleted_experience_removes_its_projection(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        store_run(knowledge_runtime, "run-1")
        experience = store_experience(knowledge_runtime, source_run_id="run-1")
        projector.project_experience(experience.id)
        assert experience.id in knowledge_index.projected

        _hard_delete(knowledge_runtime, experience.id, "run-1")

        outcome = projector.project_experience(experience.id)
        assert outcome.action is ProjectionAction.REMOVED
        assert experience.id not in knowledge_index.projected
        assert knowledge_index.deleted == [(experience.id,)]

    def test_removing_a_projection_that_does_not_exist_is_not_an_error(
        self, projector, knowledge_index
    ) -> None:
        outcome = projector.project_experience("never-projected")
        assert outcome.action is ProjectionAction.REMOVED
        assert knowledge_index.deleted == [("never-projected",)]


class TestProjectAll:
    def test_it_writes_every_experience(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        for position in range(5):
            store_experience(
                knowledge_runtime, experience_id=f"exp-{position}", title=f"t{position}"
            )
        assert projector.project_all() == 5
        assert len(knowledge_index.projected) == 5

    def test_it_pages_the_store_in_batches(self, knowledge_runtime, knowledge_index) -> None:
        for position in range(7):
            store_experience(
                knowledge_runtime, experience_id=f"exp-{position}", title=f"t{position}"
            )
        paged = KnowledgeProjector(
            knowledge_index,
            experiences=knowledge_runtime.experiences,
            sources=knowledge_runtime.experience_sources,
            runs=knowledge_runtime.runs,
            batch_size=3,
        )
        assert paged.project_all() == 7
        assert [len(written) for written, _ in knowledge_index.upserts] == [3, 3, 1]

    def test_an_empty_store_writes_nothing(self, projector, knowledge_index) -> None:
        assert projector.project_all() == 0
        assert knowledge_index.upserts == []

    def test_an_invalid_batch_size_is_refused(self, knowledge_runtime, knowledge_index) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            KnowledgeProjector(
                knowledge_index,
                experiences=knowledge_runtime.experiences,
                sources=knowledge_runtime.experience_sources,
                runs=knowledge_runtime.runs,
                batch_size=0,
            )


class TestDrift:
    def test_a_freshly_projected_index_is_clean(self, projector, knowledge_runtime) -> None:
        store_experience(knowledge_runtime)
        projector.project_all()
        report = projector.drift()
        assert report.is_clean
        assert report.store_count == 1
        assert report.index_count == 1

    def test_an_unprojected_experience_is_missing(self, projector, knowledge_runtime) -> None:
        store_experience(knowledge_runtime, experience_id="exp-1")
        report = projector.drift()
        assert report.missing == ("exp-1",)
        assert report.drifted == 1

    def test_a_changed_projection_is_stale(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        knowledge_index.corrupt(experience.id, updated_at=(NOW + timedelta(days=1)).isoformat())
        report = projector.drift()
        assert report.stale == (experience.id,)
        assert report.is_clean is False

    def test_a_projection_with_no_store_row_is_orphaned(self, projector, knowledge_index) -> None:
        knowledge_index.upsert_experiences((indexed_experience(id="ghost"),), ())
        report = projector.drift()
        assert report.orphaned == ("ghost",)
        assert report.is_clean is False

    def test_a_store_write_after_projection_shows_up_as_drift(
        self, projector, knowledge_runtime
    ) -> None:
        experience = store_experience(knowledge_runtime)
        projector.project_experience(experience.id)
        assert projector.drift().is_clean

        knowledge_runtime.experiences.update(
            experience.model_copy(update={"updated_at": NOW + timedelta(hours=1)})
        )
        assert projector.drift().stale == (experience.id,)


class TestRebuild:
    def test_an_in_memory_index_is_rebuilt_in_place(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        for position in range(3):
            store_experience(
                knowledge_runtime, experience_id=f"exp-{position}", title=f"t{position}"
            )

        report = projector.rebuild()

        assert knowledge_index.resets == 1
        assert report.projected == 3
        assert report.swapped is False
        assert report.batches == 1
        assert "3 experiences" in report.description

    def test_a_rebuild_makes_drift_disappear(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        store_experience(knowledge_runtime, experience_id="exp-1")
        store_experience(knowledge_runtime, experience_id="exp-2", title="t2")
        assert projector.drift().missing == ("exp-1", "exp-2")

        projector.rebuild()

        assert projector.drift().is_clean

    def test_it_refuses_to_trust_a_projection_that_does_not_validate(
        self, knowledge_runtime
    ) -> None:
        """The gate that makes a rebuild safe: counts and fingerprints, before trust."""
        for position in range(4):
            store_experience(
                knowledge_runtime, experience_id=f"exp-{position}", title=f"t{position}"
            )

        projector = KnowledgeProjector(
            LosingIndex(),
            experiences=knowledge_runtime.experiences,
            sources=knowledge_runtime.experience_sources,
            runs=knowledge_runtime.runs,
        )
        with pytest.raises(ProjectionError, match="does not match the store"):
            projector.rebuild()

    def test_a_rebuild_never_touches_the_store(self, projector, knowledge_runtime) -> None:
        experience = store_experience(knowledge_runtime)
        projector.rebuild()
        assert knowledge_runtime.experiences.get(experience.id) is not None

    def test_a_projection_failure_does_not_lose_the_experience(
        self, knowledge_runtime, knowledge_index
    ) -> None:
        """Section 56: an index failure must not become a store failure."""
        experience = store_experience(knowledge_runtime)
        knowledge_index._fail_on_upsert = ProjectionError("index is unreachable")
        with pytest.raises(ProjectionError):
            knowledge_runtime.knowledge_projector.project_experience(experience.id)
        assert knowledge_runtime.experiences.get(experience.id) is not None


class TestStagingArtifactGuard:
    """The cleanup helper deletes a tree, so it only accepts names it chose itself."""

    def test_it_refuses_a_directory_that_is_not_ours(self, tmp_path: Path) -> None:
        victim = tmp_path / "important"
        victim.mkdir()
        (victim / "data.txt").write_text("do not delete me", encoding="utf-8")
        with pytest.raises(ProjectionError, match="Refusing to remove"):
            _remove_index_artifacts(victim)
        assert (victim / "data.txt").exists()

    def test_it_removes_a_staging_directory(self, tmp_path: Path) -> None:
        staging = tmp_path / "aer-knowledge.rebuilding"
        staging.mkdir()
        (staging / "junk").write_text("x", encoding="utf-8")
        _remove_index_artifacts(staging)
        assert not staging.exists()

    def test_it_removes_the_staging_metadata_too(self, tmp_path: Path) -> None:
        """The leftover the production acceptance run actually found.

        The sidecar is named after the database path, so renaming
        ``aer-knowledge.rebuilding`` into place leaves
        ``aer-knowledge.rebuilding.projection.json`` behind -- a stale schema version
        sitting next to the live one, which is the first thing an operator reads when
        something is wrong.
        """
        staging = tmp_path / "aer-knowledge.rebuilding"
        staging.mkdir()
        sidecar = Path(projection_metadata_path(str(staging)))
        sidecar.write_text('{"projection_schema_version": 1}', encoding="utf-8")

        _remove_index_artifacts(staging)

        assert not staging.exists()
        assert not sidecar.exists()

    def test_a_missing_artifact_is_not_an_error(self, tmp_path: Path) -> None:
        _remove_index_artifacts(tmp_path / "aer-knowledge.previous")

    def test_an_orphaned_sidecar_is_removed_even_without_its_directory(
        self, tmp_path: Path
    ) -> None:
        """The exact state a completed swap leaves behind.

        The staging *directory* is renamed into place, so only its sidecar survives --
        and that sidecar carries a schema version identical in name to the live one.
        This is the file the production acceptance run twice found in
        ``/srv/aer/knowledge``.
        """
        staging = tmp_path / "aer-knowledge.rebuilding"
        sidecar = Path(projection_metadata_path(str(staging)))
        sidecar.write_text('{"projection_schema_version": 1}', encoding="utf-8")
        assert not staging.exists()

        _remove_index_artifacts(staging)

        assert not sidecar.exists()

    def test_a_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "aer-knowledge.rebuilding"
        path.write_text("not a directory", encoding="utf-8")
        with pytest.raises(ProjectionError, match="not a directory"):
            _remove_index_artifacts(path)


class TestRunProjection:
    def test_it_projects_the_experiences_a_run_supports(
        self, projector, knowledge_runtime, knowledge_index
    ) -> None:
        store_run(knowledge_runtime, "run-1")
        experience = store_experience(knowledge_runtime, source_run_id="run-1")
        outcomes = projector.project_run("run-1")
        assert [outcome.experience_id for outcome in outcomes] == [experience.id]
        assert experience.id in knowledge_index.projected

    def test_a_run_with_no_experiences_projects_nothing(self, projector, knowledge_runtime) -> None:
        store_run(knowledge_runtime, "run-1")
        assert projector.project_run("run-1") == ()


class LosingIndex(RecordingIndex):
    """An index that silently drops the last record of every batch.

    Stands in for a write that appeared to succeed and did not -- the exact case the
    rebuild's validation gate exists for.
    """

    def upsert_experiences(self, experiences, run_refs) -> None:  # type: ignore[no-untyped-def]
        super().upsert_experiences(experiences[:-1] if experiences else experiences, run_refs)


def _hard_delete(runtime: AER, experience_id: str, run_id: str) -> None:
    """Delete a row straight from the store, for the deletion path.

    The repository has no delete because nothing in AER deletes experiences; the
    projector's removal branch exists for the day something does, so the test
    reaches past the API rather than adding one that has no caller.
    """
    with runtime.database.session("test delete experience") as session:
        row = session.get(ExperienceRow, experience_id)
        if row is not None:
            session.delete(row)
        link = session.get(ExperienceSourceRow, (experience_id, run_id))
        if link is not None:
            session.delete(link)
