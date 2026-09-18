"""Policy and assembly: which records may be seen, and how the answer is shaped.

The recording index makes the policy observable as *data* -- the filters that were
sent -- instead of as an outcome that depends on the engine's relevance model. That
is the point of splitting the retriever from the index: "did a GUIDANCE query ask
for unverified records" is a question about this project, and it should have a
deterministic answer on any machine.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aer.exceptions import KnowledgeQueryError
from aer.knowledge.models import ExperienceSearchQuery
from aer.knowledge.retriever import ExperienceRetriever, RetrievalPolicy
from aer.runtime.enums import ExperienceKind, RetrievalMode
from tests.knowledge.support import RecordingIndex, indexed_match

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def retriever(index: RecordingIndex) -> ExperienceRetriever:
    return ExperienceRetriever(index, clock=lambda: NOW)


class TestQueryValidation:
    def test_an_empty_query_is_refused(self) -> None:
        with pytest.raises(KnowledgeQueryError, match="non-empty"):
            ExperienceSearchQuery(query="   ")

    def test_the_limit_is_clamped_into_range(self) -> None:
        assert ExperienceSearchQuery(query="x", limit=0).effective_limit == 1
        assert ExperienceSearchQuery(query="x", limit=99).effective_limit == 5
        assert ExperienceSearchQuery(query="x", limit=3).effective_limit == 3

    def test_the_default_mode_is_guidance(self) -> None:
        assert ExperienceSearchQuery(query="x").mode is RetrievalMode.GUIDANCE


class TestPolicy:
    """The filters, as values."""

    def test_guidance_asks_for_confirmed_outcomes_only(self) -> None:
        filters = RetrievalPolicy(mode=RetrievalMode.GUIDANCE).guidance_filters()
        assert filters.kinds == ("SUCCESS", "RECOVERY")
        assert filters.outcome_verified is True
        assert filters.statuses == (
            "VERIFIED",
            "REUSED",
            "PROVEN",
            "TRAINING_CANDIDATE",
            "TRAINING_DATA",
        )
        assert "DEPRECATED" not in filters.statuses

    def test_failures_are_never_guidance(self) -> None:
        filters = RetrievalPolicy(mode=RetrievalMode.GUIDANCE).failure_filters()
        assert filters.kinds == ("FAILURE",)
        assert filters.outcome_verified is None

    def test_deprecated_is_absent_unless_asked_for_in_all(self) -> None:
        for mode in (RetrievalMode.GUIDANCE, RetrievalMode.DIAGNOSTIC):
            policy = RetrievalPolicy(mode=mode, include_deprecated=True)
            assert "DEPRECATED" not in policy.guidance_filters().statuses
            assert "DEPRECATED" not in policy.failure_filters().statuses

        permissive = RetrievalPolicy(mode=RetrievalMode.ALL, include_deprecated=True)
        assert "DEPRECATED" in permissive.guidance_filters().statuses
        assert "DEPRECATED" in permissive.failure_filters().statuses

    def test_include_deprecated_without_all_is_ignored(self) -> None:
        """Both conditions, so a caller cannot widen a guidance query by accident."""
        policy = RetrievalPolicy(mode=RetrievalMode.GUIDANCE, include_deprecated=True)
        assert policy.wants_deprecated is False

    def test_only_the_wider_modes_inspect_observations(self) -> None:
        assert RetrievalPolicy(mode=RetrievalMode.GUIDANCE).inspects_observations is False
        assert RetrievalPolicy(mode=RetrievalMode.DIAGNOSTIC).inspects_observations is True
        assert RetrievalPolicy(mode=RetrievalMode.ALL).inspects_observations is True

    def test_observations_are_the_complement_of_guidance(self) -> None:
        """Two filters, because the complement is a disjunction.

        Together they cover every non-failure record that guidance does not:
        a status below VERIFIED (whatever the outcome flag), and a status at or
        above VERIFIED whose outcome was never confirmed.
        """
        filters = RetrievalPolicy(mode=RetrievalMode.DIAGNOSTIC).observation_filters()
        assert len(filters) == 2
        assert filters[0].statuses == ("RAW", "DISTILLED")
        assert filters[0].outcome_verified is None
        assert (
            filters[1].statuses
            == RetrievalPolicy(mode=RetrievalMode.GUIDANCE).guidance_filters().statuses
        )
        assert filters[1].outcome_verified is False


class TestQueriesIssued:
    """How many round trips, and in what order."""

    def test_guidance_mode_issues_two_queries(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(ExperienceSearchQuery(query="WordPress"))
        assert len(index.searches) == 2

    def test_diagnostic_mode_issues_four(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(
            ExperienceSearchQuery(query="WordPress", mode=RetrievalMode.DIAGNOSTIC)
        )
        assert len(index.searches) == 4

    def test_the_order_is_guidance_failures_observations(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(
            ExperienceSearchQuery(query="WordPress", mode=RetrievalMode.DIAGNOSTIC)
        )
        kinds = [query.filters.kinds for query in index.searches]
        assert kinds == [
            ("SUCCESS", "RECOVERY"),
            ("FAILURE",),
            ("SUCCESS", "RECOVERY"),
            ("SUCCESS", "RECOVERY"),
        ]

    def test_every_query_carries_the_text_and_the_limit(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(ExperienceSearchQuery(query="WordPress", limit=2))
        assert {query.text for query in index.searches} == {"WordPress"}
        assert {query.limit for query in index.searches} == {2}

    def test_the_domain_reaches_the_index_as_a_graph_filter(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(ExperienceSearchQuery(query="WordPress", domain="wordpress"))
        assert {query.domain for query in index.searches} == {"wordpress"}

    def test_an_unspecified_domain_is_not_sent(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(ExperienceSearchQuery(query="WordPress"))
        assert {query.domain for query in index.searches} == {None}


class TestAssembly:
    """Ranking, splitting and the source-count batch."""

    def test_guidance_is_ranked_by_text_relevance(self) -> None:
        index = RecordingIndex(
            search_results=[
                [
                    indexed_match(experience_id="weak", bm25_score=-0.4),
                    indexed_match(experience_id="strong", bm25_score=-3.0),
                ],
                [],
            ]
        )
        result = retriever(index).retrieve(ExperienceSearchQuery(query="WordPress"))
        assert [hit.experience_id for hit in result.guidance] == ["strong", "weak"]

    def test_equal_scores_are_broken_deterministically_by_id(self) -> None:
        """A rebuild that reorders identical results would make the drill unreadable."""
        index = RecordingIndex(
            search_results=[
                [
                    indexed_match(experience_id="b"),
                    indexed_match(experience_id="a"),
                    indexed_match(experience_id="c"),
                ],
                [],
            ]
        )
        result = retriever(index).retrieve(ExperienceSearchQuery(query="WordPress"))
        assert [hit.experience_id for hit in result.guidance] == ["a", "b", "c"]

    def test_each_class_is_truncated_to_the_limit(self) -> None:
        matches = [
            indexed_match(experience_id=f"exp-{i}", bm25_score=-float(i + 1)) for i in range(6)
        ]
        index = RecordingIndex(search_results=[list(matches), list(matches)])
        result = retriever(index).retrieve(ExperienceSearchQuery(query="WordPress", limit=2))
        assert len(result.guidance) == 2
        assert len(result.warnings) == 2

    def test_failures_land_in_warnings_with_their_own_label(self) -> None:
        index = RecordingIndex(
            search_results=[
                [],
                [indexed_match(experience_id="exp-fail", kind="FAILURE", solution=None)],
            ]
        )
        result = retriever(index).retrieve(ExperienceSearchQuery(query="WordPress"))
        assert result.guidance == ()
        assert [hit.label for hit in result.warnings] == ["Known Failure"]

    def test_an_unverified_observation_is_only_returned_when_the_mode_allows_it(self) -> None:
        observation = indexed_match(
            experience_id="exp-obs",
            status="DISTILLED",
            outcome_verified=False,
        )
        guidance_index = RecordingIndex(search_results=[[], [], [observation], []])
        guidance_run = retriever(guidance_index).retrieve(ExperienceSearchQuery(query="q"))
        assert guidance_run.warnings == ()
        assert len(guidance_index.searches) == 2

        diagnostic_index = RecordingIndex(search_results=[[], [], [observation], []])
        diagnostic_run = retriever(diagnostic_index).retrieve(
            ExperienceSearchQuery(query="q", mode=RetrievalMode.DIAGNOSTIC)
        )
        assert [hit.label for hit in diagnostic_run.warnings] == ["Unverified Recovery Observation"]

    def test_source_counts_are_fetched_in_one_batched_call(self) -> None:
        """Section 83: no per-hit round trip to the index or the store."""
        index = RecordingIndex(
            search_results=[
                [indexed_match(experience_id="exp-1"), indexed_match(experience_id="exp-2")],
                [indexed_match(experience_id="exp-3", kind="FAILURE", solution=None)],
            ]
        )
        result = retriever(index).retrieve(ExperienceSearchQuery(query="q"))
        assert len(index.source_count_calls) == 1
        assert set(index.source_count_calls[0]) == {"exp-1", "exp-2", "exp-3"}
        assert all(hit.source_count >= 0 for hit in result.all_hits)

    def test_no_source_count_query_when_nothing_matched(self) -> None:
        index = RecordingIndex()
        retriever(index).retrieve(ExperienceSearchQuery(query="q"))
        assert index.source_count_calls == []

    def test_an_empty_index_gives_an_empty_result_not_an_error(self) -> None:
        index = RecordingIndex()
        result = retriever(index).retrieve(ExperienceSearchQuery(query="q"))
        assert result.is_empty
        assert result.guidance == ()
        assert result.warnings == ()

    def test_the_result_records_what_was_asked(self) -> None:
        index = RecordingIndex()
        result = retriever(index).retrieve(
            ExperienceSearchQuery(query="WordPress 403", domain="wordpress", limit=4)
        )
        assert result.query == "WordPress 403"
        assert result.domain == "wordpress"
        assert result.mode is RetrievalMode.GUIDANCE

    def test_hits_carry_the_full_trust_evidence(self) -> None:
        index = RecordingIndex(
            search_results=[
                [
                    indexed_match(
                        experience_id="exp-1",
                        failed_attempts_text="盲目重试\n刷新缓存",
                        avoid_text="不要盲目重试",
                        root_cause="权限不足",
                    )
                ],
                [],
            ]
        )
        hit = retriever(index).retrieve(ExperienceSearchQuery(query="q")).guidance[0]
        assert hit.failed_attempts == ("盲目重试", "刷新缓存")
        assert hit.avoid == ("不要盲目重试",)
        assert hit.root_cause == "权限不足"
        assert hit.is_verified is True

    def test_an_absent_optional_field_becomes_none(self) -> None:
        """Empty string means "nothing recorded", and callers should not have to
        test for both spellings."""
        index = RecordingIndex(search_results=[[indexed_match(root_cause="", solution="")], []])
        hit = retriever(index).retrieve(ExperienceSearchQuery(query="q")).guidance[0]
        assert hit.root_cause is None
        assert hit.solution is None

    def test_freshness_is_computed_against_the_injected_clock(self) -> None:
        index = RecordingIndex(
            search_results=[
                [indexed_match(updated_at="2026-09-18T12:00:00+00:00")],
                [],
            ]
        )
        hit = retriever(index).retrieve(ExperienceSearchQuery(query="q")).guidance[0]
        assert hit.freshness_score == 1.0
        assert hit.retrieval_score > hit.text_score

    def test_a_deprecated_hit_says_it_is_deprecated(self) -> None:
        index = RecordingIndex(search_results=[[indexed_match(status="DEPRECATED")], []])
        hit = (
            retriever(index)
            .retrieve(
                ExperienceSearchQuery(query="q", mode=RetrievalMode.ALL, include_deprecated=True)
            )
            .guidance[0]
        )
        assert hit.is_deprecated is True
        assert hit.label == "Deprecated"


class TestVocabularyDrift:
    """A projection written by another build is a rebuild, not a silent skip."""

    def test_an_unknown_kind_is_reported(self) -> None:
        index = RecordingIndex(search_results=[[indexed_match(kind="SPECULATION")], []])
        with pytest.raises(Exception, match="rebuild"):
            retriever(index).retrieve(ExperienceSearchQuery(query="q"))

    def test_an_unknown_status_is_reported(self) -> None:
        index = RecordingIndex(search_results=[[indexed_match(status="ARCHIVED")], []])
        with pytest.raises(Exception, match="rebuild"):
            retriever(index).retrieve(ExperienceSearchQuery(query="q"))

    def test_a_naive_timestamp_is_reported(self) -> None:
        index = RecordingIndex(
            search_results=[[indexed_match(updated_at="2026-09-01T00:00:00")], []]
        )
        with pytest.raises(Exception, match="timezone"):
            retriever(index).retrieve(ExperienceSearchQuery(query="q"))

    def test_an_unparseable_timestamp_is_reported(self) -> None:
        index = RecordingIndex(search_results=[[indexed_match(created_at="yesterday")], []])
        with pytest.raises(Exception, match="ISO-8601"):
            retriever(index).retrieve(ExperienceSearchQuery(query="q"))


class TestKindsReachTheHit:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (ExperienceKind.SUCCESS, "Verified Success"),
            (ExperienceKind.RECOVERY, "Verified Recovery"),
            (ExperienceKind.FAILURE, "Known Failure"),
        ],
    )
    def test_each_kind_keeps_its_identity(self, kind: ExperienceKind, expected: str) -> None:
        index = RecordingIndex(search_results=[[indexed_match(kind=kind.value)], []])
        hit = retriever(index).retrieve(ExperienceSearchQuery(query="q")).guidance[0]
        assert hit.kind is kind
        assert hit.label == expected
