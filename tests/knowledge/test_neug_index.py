"""The index against the real engine.

Skipped where NeuG cannot be imported -- there is no Windows wheel -- and run for
real on Linux CI and on the deployment target. That split is deliberate: the unit
tests in this directory check AER's logic, and this file checks the *assumptions*
AER's logic rests on. A fake index cannot tell us whether BM25 points the way we
think, whether a Chinese query matches, or whether a second ``CREATE`` of the same
edge really does produce a second edge.

Every claim here was first established by a throwing-away capability probe on the
deployment target; these tests are what keeps them true.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aer.knowledge.base import (
    IndexedExperience,
    IndexedRunRef,
    IndexFilters,
    IndexQuery,
)
from aer.knowledge.neug import NeuGKnowledgeIndex
from tests.knowledge.support import indexed_experience

pytest.importorskip("neug", reason="NeuG has no wheel for this platform")

CORPUS = (
    (
        "exp-wp",
        "wordpress",
        "RECOVERY",
        "VERIFIED",
        True,
        "WordPress REST API 403",
        "WordPress REST API 403 应用密码无效，改用具备编辑权限的应用密码",
    ),
    (
        "exp-json",
        "wordpress",
        "RECOVERY",
        "VERIFIED",
        True,
        "wp-json 路由 404",
        "wp-json 路由 404，需要刷新固定链接并检查伪静态规则",
    ),
    (
        "exp-fs",
        "feishu",
        "RECOVERY",
        "VERIFIED",
        True,
        "多维表格字段类型不匹配",
        "多维表格写入失败因为字段类型不匹配，先读字段元数据",
    ),
    (
        "exp-cat",
        "seo",
        "RECOVERY",
        "VERIFIED",
        True,
        "分类页标题重复",
        "分类页标题重复导致权重稀释，改写分类页 H1",
    ),
    (
        "exp-inq",
        "seo",
        "SUCCESS",
        "VERIFIED",
        True,
        "询盘表单无记录",
        "询盘表单提交无记录，检查 Webhook 与反垃圾插件",
    ),
)


def _record(
    experience_id: str,
    domain: str,
    kind: str,
    status: str,
    outcome_verified: bool,
    title: str,
    problem: str,
    *,
    run_ids: tuple[str, ...] = (),
) -> IndexedExperience:
    return IndexedExperience(
        id=experience_id,
        kind=kind,
        status=status,
        domain=domain,
        title=title,
        problem=problem,
        root_cause="初始假设",
        solution="按经验处理",
        failed_attempts_text="盲目重试",
        avoid_text="不要盲目重试",
        outcome_verified=outcome_verified,
        generalizable=True,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        run_ids=run_ids,
    )


def seed(index: NeuGKnowledgeIndex) -> None:
    """The mixed Chinese/English corpus every retrieval test queries."""
    for experience_id, domain, kind, status, verified, title, problem in CORPUS:
        index.upsert_experiences(
            (_record(experience_id, domain, kind, status, verified, title, problem),),
            (),
        )


@pytest.fixture
def index(tmp_path: Path) -> NeuGKnowledgeIndex:
    """A real embedded index, closed at the end of the test."""
    opened = NeuGKnowledgeIndex(tmp_path / "aer-knowledge")
    try:
        yield opened
    finally:
        opened.close()


def search(index: NeuGKnowledgeIndex, text: str, **kwargs: object) -> list:
    filters = kwargs.pop("filters", IndexFilters())
    return index.search(
        IndexQuery(
            text=text,
            filters=filters,  # type: ignore[arg-type]
            limit=int(kwargs.pop("limit", 3)),  # type: ignore[arg-type]
            domain=kwargs.pop("domain", None),  # type: ignore[arg-type]
        )
    )


class TestSchema:
    def test_ensure_schema_is_repeatable(self, index: NeuGKnowledgeIndex) -> None:
        for _ in range(3):
            index.ensure_schema()
        assert index.projection_version() == 1

    def test_it_creates_its_own_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "aer-knowledge"
        with NeuGKnowledgeIndex(target) as opened:
            opened.ensure_schema()
        assert target.is_dir()

    def test_a_schema_version_this_build_does_not_know_is_refused(
        self, index: NeuGKnowledgeIndex
    ) -> None:
        from aer.exceptions import KnowledgeSchemaError

        index.ensure_schema()
        index.close()
        metadata = Path(f"{index.path}.projection.json")
        metadata.write_text('{"projection_schema_version": 99}', encoding="utf-8")

        with (
            NeuGKnowledgeIndex(index.path) as reopened,
            pytest.raises(KnowledgeSchemaError, match="rebuild"),
        ):
            reopened.ensure_schema()

    def test_an_unversioned_populated_index_is_refused(self, index: NeuGKnowledgeIndex) -> None:
        """No metadata plus existing data means the layout cannot be assumed."""
        from aer.exceptions import KnowledgeSchemaError

        index.ensure_schema()
        index.upsert_experiences((indexed_experience(),), ())
        index.close()
        Path(f"{index.path}.projection.json").unlink()

        with (
            NeuGKnowledgeIndex(index.path) as reopened,
            pytest.raises(KnowledgeSchemaError, match="rebuild"),
        ):
            reopened.ensure_schema()


class TestUpsert:
    def test_a_projected_experience_is_searchable(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        index.upsert_experiences((indexed_experience(title="WordPress REST API 403"),), ())
        assert [match.experience_id for match in search(index, "WordPress")] == ["exp-1"]

    def test_projecting_twice_leaves_one_node(self, index: NeuGKnowledgeIndex) -> None:
        """NeuG has no MERGE, so this is the property the delete-then-create buys."""
        index.ensure_schema()
        record = indexed_experience()
        index.upsert_experiences((record,), ())
        index.upsert_experiences((record,), ())
        assert index.count_experiences() == 1

    def test_projecting_twice_does_not_duplicate_edges(self, index: NeuGKnowledgeIndex) -> None:
        """A second CREATE really does produce a second edge -- verified on the engine.

        This is the reason the projector deletes the node first rather than updating
        it: without that, the source count would grow every time it ran.
        """
        index.ensure_schema()
        ref = IndexedRunRef(id="run-1", status="SUCCESS", agent_name="wp", agent_version="1")
        record = indexed_experience(run_ids=("run-1",))
        index.upsert_experiences((record,), (ref,))
        index.upsert_experiences((record,), (ref,))
        assert index.source_counts(("exp-1",)) == {"exp-1": 1}

    def test_an_update_replaces_the_content(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        index.upsert_experiences((indexed_experience(title="旧标题"),), ())
        index.upsert_experiences(
            (indexed_experience(title="新标题", updated_at="2026-10-01T00:00:00+00:00"),),
            (),
        )
        assert index.count_experiences() == 1
        assert search(index, "新标题")[0].title == "新标题"
        assert index.fingerprints() == {"exp-1": "2026-10-01T00:00:00+00:00"}

    def test_multiple_sources_are_all_kept(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        refs = tuple(
            IndexedRunRef(id=f"run-{i}", status="SUCCESS", agent_name="wp", agent_version="1")
            for i in range(3)
        )
        index.upsert_experiences((indexed_experience(run_ids=("run-0", "run-1", "run-2")),), refs)
        assert index.source_counts(("exp-1",)) == {"exp-1": 3}

    def test_a_shared_domain_does_not_collide(self, index: NeuGKnowledgeIndex) -> None:
        """Two experiences in one domain must share one ``Domain`` node.

        The engine rejects a duplicate primary key outright, so if the projector
        asked it to create the same domain twice this would raise -- which makes
        "it does not raise, and both are still findable by domain" a real test of
        de-duplication rather than a restatement of it.
        """
        index.ensure_schema()
        index.upsert_experiences(
            (
                indexed_experience(id="exp-a", domain="wordpress", title="first"),
                indexed_experience(id="exp-b", domain="wordpress", title="second"),
            ),
            (),
        )
        found = search(index, "first", domain="wordpress")
        assert [match.experience_id for match in found] == ["exp-a"]
        assert index.count_experiences() == 2

    def test_a_deprecated_experience_keeps_its_status(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        index.upsert_experiences(
            (indexed_experience(status="DEPRECATED"),),
            (),
        )
        match = search(index, "WordPress")[0]
        assert match.status == "DEPRECATED"
        assert index.count_experiences(include_deprecated=False) == 0

    def test_long_text_round_trips(self, index: NeuGKnowledgeIndex) -> None:
        """``STRING`` is ``VARCHAR(256)`` in NeuG, so the schema declares widths.

        A silently truncated problem statement would answer questions with half a
        sentence, which is worse than refusing the write.
        """
        index.ensure_schema()
        problem = "海" * 900
        index.upsert_experiences(
            (indexed_experience(title="long", problem=problem),),
            (),
        )
        assert search(index, "long")[0].problem == problem

    def test_multiline_list_text_round_trips(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        index.upsert_experiences((indexed_experience(failed_attempts_text="第一个\n第二个"),), ())
        assert search(index, "WordPress")[0].failed_attempts_text == "第一个\n第二个"


class TestDeleteAndReset:
    def test_deleting_a_projection_removes_it_from_search(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        seed(index)
        index.delete_experiences(("exp-wp",))
        assert "exp-wp" not in {match.experience_id for match in search(index, "WordPress")}

    def test_deleting_an_unknown_id_is_not_an_error(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        index.delete_experiences(("never-existed",))

    def test_reset_empties_everything_and_keeps_search_working(
        self, index: NeuGKnowledgeIndex
    ) -> None:
        """Row wipe, not ``DROP TABLE``.

        Dropping a node table takes its relationship tables and its full-text index
        with it, leaving an index that can no longer be searched.
        """
        index.ensure_schema()
        seed(index)
        index.reset()
        assert index.count_experiences() == 0
        index.upsert_experiences((indexed_experience(title="after reset"),), ())
        assert [match.experience_id for match in search(index, "after reset")] == ["exp-1"]

    def test_a_reset_clears_relations_too(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        ref = IndexedRunRef(id="run-1", status="SUCCESS", agent_name="wp", agent_version="1")
        index.upsert_experiences((indexed_experience(run_ids=("run-1",)),), (ref,))
        index.reset()
        assert index.source_counts(("exp-1",)) == {}


class TestPersistence:
    def test_projection_index_and_relations_survive_a_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "aer-knowledge"
        with NeuGKnowledgeIndex(path) as opened:
            opened.ensure_schema()
            seed(opened)
            ref = IndexedRunRef(id="run-1", status="SUCCESS", agent_name="wp", agent_version="1")
            opened.upsert_experiences(
                (indexed_experience(id="exp-linked", run_ids=("run-1",)),), (ref,)
            )

        with NeuGKnowledgeIndex(path) as reopened:
            assert reopened.projection_version() == 1
            assert reopened.count_experiences() == len(CORPUS) + 1
            assert [match.experience_id for match in search(reopened, "多维表格")] == ["exp-fs"]
            assert reopened.source_counts(("exp-linked",)) == {"exp-linked": 1}

    def test_a_link_the_store_has_forgets_is_still_reported(
        self, index: NeuGKnowledgeIndex
    ) -> None:
        index.ensure_schema()
        index.upsert_experiences((indexed_experience(run_ids=("run-gone",)),), ())
        assert index.source_counts(("exp-1",)) == {"exp-1": 0}


class TestSearch:
    def test_chinese_and_english_both_rank_their_own_record_first(
        self, index: NeuGKnowledgeIndex
    ) -> None:
        """Section 28's recall check, on the four strings the brief names."""
        index.ensure_schema()
        seed(index)
        for query, expected in (
            ("WordPress REST API 403", "exp-wp"),
            ("wp-json 404", "exp-json"),
            ("多维表格 字段类型", "exp-fs"),
            ("分类页 标题", "exp-cat"),
            ("询盘 Webhook", "exp-inq"),
        ):
            matches = search(index, query)
            assert matches[0].experience_id == expected, (query, [m.experience_id for m in matches])

    def test_a_better_match_ranks_first(self, index: NeuGKnowledgeIndex) -> None:
        """BM25 comes back negative and lower-is-better; the ranking must not care."""
        index.ensure_schema()
        seed(index)
        matches = search(index, "WordPress REST API 403")
        assert matches[0].experience_id == "exp-wp"
        assert matches[0].bm25_score < matches[-1].bm25_score

    def test_the_domain_filter_excludes_other_domains(self, index: NeuGKnowledgeIndex) -> None:
        """The graph hop doing work a text index could not do."""
        index.ensure_schema()
        seed(index)
        matches = search(index, "分类页", domain="seo")
        assert {match.experience_id for match in matches} <= {"exp-cat", "exp-inq"}

    def test_a_query_with_no_matching_terms_returns_nothing(
        self, index: NeuGKnowledgeIndex
    ) -> None:
        index.ensure_schema()
        seed(index)
        assert search(index, "完全不相干的词组xyz") == []

    @pytest.mark.parametrize(
        "hostile",
        ["wp-json 404", "zzz-nothing", "!!!", "a:b", "AND", "NOT x", "a (b)", "site-health*"],
    )
    def test_hostile_query_text_never_raises(self, index: NeuGKnowledgeIndex, hostile: str) -> None:
        """Passed through raw, seven of these made the engine throw during the probe."""
        index.ensure_schema()
        seed(index)
        search(index, hostile)

    def test_filters_are_applied_inside_the_engine(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        seed(index)
        verified = IndexFilters(
            kinds=("SUCCESS", "RECOVERY"),
            statuses=("VERIFIED",),
            outcome_verified=True,
        )
        assert search(index, "WordPress 分类页 询盘", filters=verified)
        assert not search(
            index,
            "WordPress 分类页 询盘",
            filters=IndexFilters(kinds=("FAILURE",)),
        )

    def test_the_limit_bounds_the_result(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        seed(index)
        assert len(search(index, "WordPress 分类页 询盘 多维表格 wp json", limit=2)) <= 2

    def test_matches_carry_every_field_the_formatter_needs(self, index: NeuGKnowledgeIndex) -> None:
        index.ensure_schema()
        seed(index)
        match = search(index, "WordPress")[0]
        assert match.domain == "wordpress"
        assert match.kind == "RECOVERY"
        assert match.status == "VERIFIED"
        assert match.outcome_verified is True
        assert match.failed_attempts_text == "盲目重试"
        assert match.avoid_text == "不要盲目重试"
        assert match.created_at == "2026-09-01T00:00:00+00:00"


class TestThroughput:
    def test_a_thousand_experiences_project_and_stay_searchable(
        self, index: NeuGKnowledgeIndex, tmp_path: Path
    ) -> None:
        """Section 82: catch a quadratic or per-row catastrophe, not a fake SLA.

        The measured cost of a write-bearing transaction is ~900 ms and of a
        statement ~2 ms, which is why the whole batch goes in one transaction. If
        that ever stops being true, this test is where it shows up -- as a number
        nobody would accept, not as a threshold someone picked.
        """
        index.ensure_schema()
        total = 1000
        records = tuple(
            _record(
                f"exp-{i:04d}",
                f"domain-{i % 7}",
                "RECOVERY",
                "VERIFIED",
                True,
                f"问题 {i} WordPress REST API 403",
                f"第 {i} 条经验，涉及 WordPress REST API 403 与多维表格字段类型",
            )
            for i in range(total)
        )

        started = time.perf_counter()
        for chunk in range(0, total, 500):
            index.upsert_experiences(records[chunk : chunk + 500], ())
        elapsed = time.perf_counter() - started

        assert index.count_experiences() == total
        print(f"\n  projected {total} experiences in {elapsed:.2f}s")

        started = time.perf_counter()
        matches = search(index, "WordPress REST API 403 多维表格", limit=3)
        query_elapsed = time.perf_counter() - started
        assert len(matches) == 3
        print(f"  top-3 over {total} experiences in {query_elapsed * 1000:.1f}ms")
