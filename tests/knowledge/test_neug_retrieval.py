"""The acceptance scenario, end to end through the facade.

Section 93 of the round-6 brief, in code: one verified recovery and one known
failure about the same problem, projected into a real index, retrieved through
``AER.retrieve``, and rendered with the two roles kept apart.

Then section 94: throw the entire knowledge database away and rebuild it from
SQLite, and get the same answer back. That second half is the whole milestone --
a projection that cannot be reconstructed is not a projection, it is a second
source of truth wearing a different name.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from aer import AER, ExperienceKind, ExperienceStatus, RetrievalMode
from aer.knowledge import neug as neug_module
from aer.knowledge.base import IndexedExperience, IndexFilters, IndexQuery
from aer.knowledge.formatter import ExperienceContextFormatter
from aer.knowledge.neug import NeuGKnowledgeIndex
from tests.knowledge.conftest import store_experience

pytest.importorskip("neug", reason="NeuG has no wheel for this platform")

PROBLEM = "WordPress REST API 403"
QUERY = "WordPress REST API 403"

#: Every filter that could match, for tests that want the engine unnoticeably open.
NO_FILTERS = IndexFilters(
    kinds=("SUCCESS", "RECOVERY", "FAILURE"),
    statuses=("RAW", "DISTILLED", "VERIFIED", "DEPRECATED"),
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[AER]:
    """A real runtime with a real embedded knowledge index."""
    opened = AER(tmp_path / "data", knowledge_dir=tmp_path / "knowledge")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def populated(runtime: AER) -> AER:
    """The brief's corpus: one recovery that worked, one failure that did not."""
    store_experience(
        runtime,
        experience_id="exp-recovery",
        kind=ExperienceKind.RECOVERY,
        status=ExperienceStatus.VERIFIED,
        title=PROBLEM,
        problem=f"{PROBLEM} 应用密码无效，改用具备编辑权限的应用密码",
        failed_attempts=("盲目重试",),
        solution="检查 capability 与凭据后改用有 edit_posts 权限的应用密码",
        avoid=("不要盲目重试",),
        outcome_verified=True,
    )
    store_experience(
        runtime,
        experience_id="exp-failure",
        kind=ExperienceKind.FAILURE,
        status=ExperienceStatus.VERIFIED,
        title=PROBLEM,
        problem=f"{PROBLEM} 盲目重试无效",
        failed_attempts=("盲目重试", "重启服务"),
        root_cause="应用密码缺少 edit_posts",
        solution=None,
        outcome_verified=True,
    )
    runtime.project_experiences()
    return runtime


class TestCoreScenario:
    def test_guidance_and_warnings_are_kept_apart(self, populated: AER) -> None:
        result = populated.retrieve(QUERY, domain="wordpress")

        assert [hit.experience_id for hit in result.guidance] == ["exp-recovery"]
        assert [hit.experience_id for hit in result.warnings] == ["exp-failure"]
        assert result.guidance[0].label == "Verified Recovery"
        assert result.warnings[0].label == "Known Failure"

    def test_the_failure_is_never_offered_as_a_solution(self, populated: AER) -> None:
        rendered = ExperienceContextFormatter().format(
            populated.retrieve(QUERY, domain="wordpress")
        )
        failure_block = rendered.split("[Known Failure]")[1]
        assert "Solution" not in failure_block
        assert "Proven" not in failure_block
        assert "Hypothesized cause" in failure_block

    def test_the_rendered_context_says_what_worked_and_what_did_not(self, populated: AER) -> None:
        rendered = populated.experience_context(QUERY, domain="wordpress")
        assert "[Verified Recovery]" in rendered
        assert "[Known Failure]" in rendered
        assert "edit_posts" in rendered

    def test_a_domain_filter_excludes_another_domain(self, populated: AER) -> None:
        assert populated.retrieve(QUERY, domain="seo").is_empty

    def test_a_cross_domain_search_still_finds_it(self, populated: AER) -> None:
        result = populated.retrieve(QUERY)
        assert result.guidance
        assert result.guidance[0].domain == "wordpress"

    def test_diagnostic_mode_widens_without_changing_the_roles(self, populated: AER) -> None:
        result = populated.retrieve(QUERY, mode=RetrievalMode.DIAGNOSTIC)
        assert result.guidance[0].label == "Verified Recovery"
        assert result.warnings[0].label == "Known Failure"

    def test_a_query_with_no_relevant_experience_returns_nothing(self, populated: AER) -> None:
        assert populated.retrieve("完全不相关的主题 xyzzy").is_empty

    def test_source_count_comes_from_the_graph(self, populated: AER) -> None:
        hit = populated.retrieve(QUERY, domain="wordpress").guidance[0]
        assert hit.source_count == 0


class TestDatabaseLoss:
    """Section 75: the knowledge database is disposable. Prove it."""

    def test_the_whole_index_can_be_deleted_and_rebuilt(self, populated: AER) -> None:
        before = populated.retrieve(QUERY, domain="wordpress")
        index_path = Path(populated.knowledge_index.path)

        populated.knowledge_index.close()
        shutil.rmtree(index_path)
        assert not index_path.exists()

        report = populated.rebuild_knowledge()

        after = populated.retrieve(QUERY, domain="wordpress")
        assert report.projected == 2
        assert [hit.experience_id for hit in after.all_hits] == [
            hit.experience_id for hit in before.all_hits
        ]

    def test_a_rebuild_does_not_touch_the_store(self, populated: AER) -> None:
        before = {
            experience.id: experience.updated_at
            for experience in populated.experiences.list(include_deprecated=True)
        }

        populated.rebuild_knowledge()

        after = {
            experience.id: experience.updated_at
            for experience in populated.experiences.list(include_deprecated=True)
        }
        assert after == before

    def test_the_status_reports_the_repair(self, populated: AER) -> None:
        index_path = Path(populated.knowledge_index.path)
        populated.knowledge_index.close()
        shutil.rmtree(index_path)

        populated.rebuild_knowledge()

        status = populated.knowledge_status()
        assert status.reachable is True
        assert status.in_sync is True
        assert status.store_experiences == 2
        assert status.index_experiences == 2


class TestRebuildAcceptance:
    """Section 94: same ids, same kinds, same roles after a rebuild."""

    def test_a_rebuild_preserves_the_answer(self, populated: AER) -> None:
        before = populated.retrieve(QUERY, domain="wordpress")
        populated.rebuild_knowledge()
        after = populated.retrieve(QUERY, domain="wordpress")

        assert _identity(before) == _identity(after)

    def test_a_rebuild_preserves_the_ranking(self, populated: AER) -> None:
        before = populated.retrieve(QUERY, domain="wordpress")
        populated.rebuild_knowledge()
        after = populated.retrieve(QUERY, domain="wordpress")

        assert [hit.retrieval_score for hit in before.all_hits] == pytest.approx(
            [hit.retrieval_score for hit in after.all_hits]
        )

    def test_sqlite_wins_over_an_edited_knowledge_database(self, populated: AER) -> None:
        """Section 69, and the property that makes having an index safe at all."""
        index = _as_neug(populated)
        original = _search(index, "WordPress")[0]

        index.upsert_experiences((_with_title(original, "被篡改的标题"),), ())
        assert _search(index, "被篡改")

        populated.rebuild_knowledge()

        repaired = _search(index, QUERY)
        assert {match.title for match in repaired} == {PROBLEM}

    def test_drift_is_reported_before_the_rebuild_and_gone_after(self, populated: AER) -> None:
        store_experience(
            populated,
            experience_id="exp-late",
            title="后来才写入的经验",
            problem="后来才写入的经验，需要增量投影",
        )
        drift = populated.knowledge_status().drift
        assert drift is not None
        assert drift.missing == ("exp-late",)

        populated.rebuild_knowledge()

        assert populated.knowledge_status().in_sync is True


class TestRuntimeCompatibility:
    """Sections 16 and 17: after a rebuild the runtime both reads and writes."""

    def test_new_experiences_can_be_added_after_a_rebuild(self, populated: AER) -> None:
        populated.rebuild_knowledge()

        store_experience(
            populated,
            experience_id="exp-new",
            title="新问题 询盘表单",
            problem="询盘表单提交无记录，检查 Webhook",
            domain="seo",
        )
        populated.project_experience("exp-new")

        result = populated.retrieve("询盘表单 Webhook", domain="seo")
        assert [hit.experience_id for hit in result.guidance] == ["exp-new"]
        assert populated.knowledge_status().in_sync is True

    def test_old_and_new_experiences_coexist(self, populated: AER) -> None:
        store_experience(
            populated,
            experience_id="exp-new",
            title="新问题 询盘表单",
            problem="询盘表单提交无记录，检查 Webhook",
            domain="seo",
        )
        populated.project_experience("exp-new")

        assert populated.knowledge_status().index_experiences == 3
        assert populated.retrieve(QUERY, domain="wordpress").guidance
        assert populated.retrieve("询盘表单 Webhook", domain="seo").guidance


class TestUnavailableIndex:
    """Section 80: broken must not look like empty."""

    def test_retrieval_raises_rather_than_returning_nothing(
        self, populated: AER, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The distinction an agent cannot make for itself if we lie to it."""
        from aer.exceptions import KnowledgeIndexUnavailable

        populated.knowledge_index.close()

        def explode(*args: object, **kwargs: object) -> NeuGKnowledgeIndex:
            raise KnowledgeIndexUnavailable("knowledge index is unreachable")

        monkeypatch.setattr(neug_module, "NeuGKnowledgeIndex", explode)
        populated._knowledge_index = None
        populated._retriever = None

        with pytest.raises(KnowledgeIndexUnavailable):
            populated.retrieve(QUERY)

    def test_the_store_still_works_without_the_knowledge_plane(self, tmp_path: Path) -> None:
        """Section 79: a broken knowledge plane must not stop runs being recorded."""
        runtime = AER(tmp_path / "data", knowledge_dir=tmp_path / "knowledge")
        try:
            run = runtime.start_run(task="knowledge is not needed for this")
            run.success()
            assert runtime.runs.count() == 1
        finally:
            runtime.close()


def _as_neug(runtime: AER) -> NeuGKnowledgeIndex:
    index = runtime.knowledge_index
    assert isinstance(index, NeuGKnowledgeIndex)
    return index


def _search(index: NeuGKnowledgeIndex, text: str) -> list:
    return index.search(IndexQuery(text=text, filters=NO_FILTERS, limit=5))


def _with_title(match: object, title: str) -> IndexedExperience:
    """The stored match, with one field changed, as the engine would hold it."""
    return IndexedExperience(
        id=match.experience_id,  # type: ignore[attr-defined]
        kind=match.kind,  # type: ignore[attr-defined]
        status=match.status,  # type: ignore[attr-defined]
        domain=match.domain,  # type: ignore[attr-defined]
        title=title,
        problem=match.problem,  # type: ignore[attr-defined]
        root_cause=match.root_cause,  # type: ignore[attr-defined]
        solution=match.solution,  # type: ignore[attr-defined]
        failed_attempts_text=match.failed_attempts_text,  # type: ignore[attr-defined]
        avoid_text=match.avoid_text,  # type: ignore[attr-defined]
        outcome_verified=match.outcome_verified,  # type: ignore[attr-defined]
        generalizable=match.generalizable,  # type: ignore[attr-defined]
        created_at=match.created_at,  # type: ignore[attr-defined]
        updated_at=match.updated_at,  # type: ignore[attr-defined]
    )


def _identity(result: object) -> list[tuple[str, str, str]]:
    """The part of a retrieval answer that must be identical across a rebuild."""
    return [
        (hit.experience_id, hit.kind.value, hit.label)
        for hit in result.all_hits  # type: ignore[attr-defined]
    ]
