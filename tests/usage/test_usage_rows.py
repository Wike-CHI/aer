"""Usage rows: one per experience per retrieval, with the ranking evidence kept.

The rank, the role and the score are stored **as they were** (section 60). Re-deriving
them later from the current ranker and policy would invent a past that never happened,
and the first thing to drift would be the ordering that a report's numbers came from.
"""

from __future__ import annotations

import pytest

from aer import AER, ExperienceKind, ExperienceStatus, RetrievalMode, UsageTrackingError
from tests.knowledge.support import RecordingIndex
from tests.usage.support import indexed_for, retrieve


class TestOneRowPerHit:
    def test_every_hit_gets_exactly_one_row(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        first = indexed_for(usage_runtime, experience_id="exp-1")
        second = indexed_for(usage_runtime, experience_id="exp-2", status=ExperienceStatus.REUSED)

        tracked = retrieve(usage_runtime, index, guidance=[first, second])

        rows = usage_runtime.get_session_usage(tracked.session_id)
        assert [row.experience_id for row in rows] == ["exp-1", "exp-2"]
        assert usage_runtime.experience_usage.count(session_id=tracked.session_id) == 2

    def test_the_rows_are_recorded_as_injected_only_later(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Retrieval is exposure, not use: nothing downstream is set yet."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        (row,) = usage_runtime.get_session_usage(tracked.session_id)
        assert row.is_injected is False
        assert row.injected_at is None
        assert row.usage_signal.value == "UNKNOWN"
        assert row.usage_signal_source is None
        assert row.utility_label.value == "UNKNOWN"


class TestTheRankingEvidenceIsPreserved:
    def test_rank_follows_the_order_the_agent_sees(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """One guidance hit and two warnings: the rank runs across the whole result,
        guidance first, which is exactly how the formatter renders it."""
        guidance = indexed_for(usage_runtime, experience_id="exp-g")
        failure = indexed_for(
            usage_runtime,
            experience_id="exp-fail",
            kind=ExperienceKind.FAILURE,
            outcome_verified=False,
        )
        warning = indexed_for(usage_runtime, experience_id="exp-w")

        tracked = retrieve(usage_runtime, index, guidance=[guidance], failures=[warning, failure])

        rows = usage_runtime.get_session_usage(tracked.session_id)
        assert [row.rank for row in rows] == [1, 2, 3]
        assert rows[0].experience_id == "exp-g"
        assert rows[0].role.value == "GUIDANCE"
        assert {row.experience_id for row in rows[1:]} == {"exp-w", "exp-fail"}
        # The ranks are contiguous and match the order the hits were returned in.
        assert [row.experience_id for row in rows] == [
            hit.experience_id for hit in tracked.result.all_hits
        ]

    def test_role_is_the_slot_not_a_re_derivation(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 9: guidance, a confirmed failure and an unverified observation are
        three different things, and the row says which one it was."""
        guidance = indexed_for(usage_runtime, experience_id="exp-good")
        failure = indexed_for(
            usage_runtime,
            experience_id="exp-bad",
            kind=ExperienceKind.FAILURE,
            outcome_verified=True,
        )
        observation = indexed_for(
            usage_runtime,
            experience_id="exp-maybe",
            status=ExperienceStatus.DISTILLED,
            outcome_verified=False,
        )

        tracked = retrieve(
            usage_runtime,
            index,
            guidance=[guidance],
            failures=[failure],
            observations=[observation],
            mode=RetrievalMode.DIAGNOSTIC,
        )

        roles = {
            row.experience_id: row.role.value
            for row in usage_runtime.get_session_usage(tracked.session_id)
        }
        assert roles == {
            "exp-good": "GUIDANCE",
            "exp-bad": "WARNING",
            "exp-maybe": "OBSERVATION",
        }

    def test_the_retrieval_score_is_the_one_the_ranker_produced(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)

        tracked = retrieve(usage_runtime, index, guidance=[hit])

        (row,) = usage_runtime.get_session_usage(tracked.session_id)
        assert row.retrieval_score == pytest.approx(tracked.result.all_hits[0].retrieval_score)


class TestADuplicateIsRefused:
    def test_the_same_experience_twice_in_one_result_fails_loudly(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 49: one experience, one row. A duplicate means the retriever
        returned one record in two slots, and a quiet de-duplication would hide it."""
        hit = indexed_for(usage_runtime)

        with pytest.raises(UsageTrackingError, match="NOT tracked"):
            retrieve(usage_runtime, index, guidance=[hit, hit])

        # And nothing was half-written: the session and its rows are all-or-nothing.
        assert usage_runtime.retrieval_sessions.count() == 0
        assert usage_runtime.experience_usage.count() == 0


class TestTrackingFailureIsLoud:
    def test_a_write_failure_is_reported_as_untracked(
        self, usage_runtime: AER, index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section 51: retrieval succeeded, tracking did not, and the caller is told.

        A silent success here would be the worst possible outcome -- the caller would
        believe an exposure was recorded when the evidence base had never heard of it.
        """
        from aer.exceptions import StorageError

        hit = indexed_for(usage_runtime)

        def explode(*args: object, **kwargs: object) -> None:
            raise StorageError("the usage store is unavailable")

        monkeypatch.setattr(usage_runtime.retrieval_sessions, "create", explode)

        with pytest.raises(UsageTrackingError, match="NOT tracked"):
            retrieve(usage_runtime, index, guidance=[hit])
