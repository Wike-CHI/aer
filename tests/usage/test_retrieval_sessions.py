"""Retrieval sessions: recording that a search happened (sections 5-7, 63).

The assertions here are mostly about *absence* being a fact. "We searched and found
nothing" has to be storable, and "we never searched" has to be distinguishable from
it -- which is the whole reason a session is a row rather than a counter.
"""

from __future__ import annotations

import pytest

from aer import AER, RecordNotFoundError, RetrievalMode
from aer.usage.fingerprints import RETRIEVAL_QUERY_MAX_LENGTH, query_fingerprint, sanitize_query
from tests.knowledge.support import RecordingIndex
from tests.usage.support import DEFAULT_QUERY, indexed_for, retrieve, start_run


class TestTrackedRetrievalCreatesASession:
    def test_one_session_per_retrieval(self, usage_runtime: AER, index: RecordingIndex) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.result_count == 1
        assert session.query_text == DEFAULT_QUERY
        assert session.domain == "wordpress"
        assert session.mode is RetrievalMode.GUIDANCE
        assert session.requested_limit == 3
        assert usage_runtime.retrieval_sessions.count() == 1

    def test_the_session_records_which_run_it_was_for(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        run_id = start_run(usage_runtime)

        tracked = retrieve(
            usage_runtime, index, guidance=[indexed_for(usage_runtime)], run_id=run_id
        )

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.run_id == run_id
        assert session.is_attached is True

    def test_the_session_records_the_policy_and_projection_versions(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """So that "why did this come back then?" survives a policy change."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.retrieval_policy_version != ""
        assert session.knowledge_projection_version == index.projection_version()

    def test_an_unknown_run_is_refused_before_anything_is_written(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        with pytest.raises(RecordNotFoundError):
            retrieve(
                usage_runtime, index, guidance=[indexed_for(usage_runtime)], run_id="no-such-run"
            )

        assert usage_runtime.retrieval_sessions.count() == 0


class TestZeroResults:
    def test_an_empty_result_still_records_a_session(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 7: "we looked and there was nothing" is a fact about the store."""
        tracked = retrieve(usage_runtime, index)

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.result_count == 0
        assert session.found_nothing is True
        assert tracked.is_empty is True
        assert usage_runtime.get_session_usage(session.id) == []

    def test_an_empty_result_is_distinguishable_from_never_searching(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        assert usage_runtime.retrieval_sessions.count() == 0

        retrieve(usage_runtime, index)

        sessions = usage_runtime.list_retrieval_sessions()
        assert [session.result_count for session in sessions] == [0]


class TestPureRetrievalStaysPure:
    def test_retrieve_writes_nothing(self, usage_runtime: AER, index: RecordingIndex) -> None:
        """Section 17: dashboards, debugging and tests must not pollute the data."""
        hit = indexed_for(usage_runtime)
        index.script_results([[hit], []])

        result = usage_runtime.retrieve(DEFAULT_QUERY, domain="wordpress")

        assert len(result.all_hits) == 1
        assert usage_runtime.retrieval_sessions.count() == 0
        assert usage_runtime.experience_usage.count() == 0

    def test_tracked_and_untracked_retrieval_agree(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Tracking must not change the answer, or the data would describe a
        different search than the caller performed."""
        hit = indexed_for(usage_runtime)
        index.script_results([[hit], []])
        untracked = usage_runtime.retrieve(DEFAULT_QUERY, domain="wordpress")

        tracked = retrieve(usage_runtime, index, guidance=[hit])

        assert [h.experience_id for h in tracked.result.all_hits] == [
            h.experience_id for h in untracked.all_hits
        ]
        # Approximately, because the score carries a freshness term that is a
        # function of when it was computed (aer.knowledge.ranking.freshness_score).
        # The tracked and untracked paths must agree on the answer, not on the
        # nanosecond they asked for it.
        assert [h.retrieval_score for h in tracked.result.all_hits] == [
            pytest.approx(h.retrieval_score) for h in untracked.all_hits
        ]


class TestAttachingALateRun:
    def test_a_session_can_start_unattached(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 24: retrieval may happen before the run it informs exists."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.run_id is None

    def test_attaching_links_it_afterwards(self, usage_runtime: AER, index: RecordingIndex) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])
        run_id = start_run(usage_runtime, task="the run that needed the guidance")

        attached = usage_runtime.attach_session_to_run(tracked.session_id, run_id)

        assert attached.run_id == run_id
        reloaded = usage_runtime.get_retrieval_session(tracked.session_id)
        assert reloaded is not None
        assert reloaded.run_id == run_id

    def test_attaching_the_same_run_twice_is_idempotent(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])
        run_id = start_run(usage_runtime)

        first = usage_runtime.attach_session_to_run(tracked.session_id, run_id)
        second = usage_runtime.attach_session_to_run(tracked.session_id, run_id)

        assert first == second

    def test_repointing_a_session_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Re-attaching would silently reattribute every usage row beneath it."""
        from aer import UsageTrackingError

        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])
        original = start_run(usage_runtime, task="the original run")
        other = start_run(usage_runtime, task="a different run")
        usage_runtime.attach_session_to_run(tracked.session_id, original)

        with pytest.raises(UsageTrackingError, match="already attached"):
            usage_runtime.attach_session_to_run(tracked.session_id, other)

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.run_id == original

    def test_attaching_an_unknown_run_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        with pytest.raises(RecordNotFoundError):
            usage_runtime.attach_session_to_run(tracked.session_id, "no-such-run")

    def test_attaching_an_unknown_session_is_refused(self, usage_runtime: AER) -> None:
        run_id = start_run(usage_runtime)

        with pytest.raises(RecordNotFoundError):
            usage_runtime.attach_session_to_run("no-such-session", run_id)


class TestTheQueryIsSanitized:
    def test_a_credential_in_the_query_is_redacted(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 6: only the retrieval question is stored, never the prompt."""
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUV"
        tracked = retrieve(
            usage_runtime,
            index,
            guidance=[indexed_for(usage_runtime)],
            query=f"why does the call fail with api_key={secret}?",
        )

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert secret not in session.query_text
        assert "[REDACTED]" in session.query_text

    def test_a_long_query_is_capped(self, usage_runtime: AER, index: RecordingIndex) -> None:
        tracked = retrieve(
            usage_runtime, index, guidance=[indexed_for(usage_runtime)], query="x" * 5000
        )

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert len(session.query_text) <= RETRIEVAL_QUERY_MAX_LENGTH

    def test_line_breaks_are_collapsed(self, usage_runtime: AER, index: RecordingIndex) -> None:
        tracked = retrieve(
            usage_runtime,
            index,
            guidance=[indexed_for(usage_runtime)],
            query="first line\n\nsecond line",
        )

        session = usage_runtime.get_retrieval_session(tracked.session_id)
        assert session is not None
        assert session.query_text == "first line second line"

    def test_the_fingerprint_groups_the_same_question(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Case and spacing must not split one recurring problem into many."""
        hit = indexed_for(usage_runtime)

        first = retrieve(usage_runtime, index, guidance=[hit], query="REST API 403")
        second = retrieve(usage_runtime, index, guidance=[hit], query="  rest   api 403 ")

        one = usage_runtime.get_retrieval_session(first.session_id)
        two = usage_runtime.get_retrieval_session(second.session_id)
        assert one is not None and two is not None
        assert one.query_fingerprint == two.query_fingerprint
        assert usage_runtime.retrieval_sessions.count(query_fingerprint=one.query_fingerprint) == 2

    def test_the_fingerprint_of_redacted_text_is_stable(self) -> None:
        """Derived after redaction, so a pasted token cannot change the grouping."""
        with_secret = sanitize_query("fail with api_key=sk-ABCDEFGHIJKLMNOPQRSTUV")
        redacted = sanitize_query("fail with api_key=[REDACTED]")

        assert with_secret == redacted
        assert query_fingerprint(with_secret) == query_fingerprint(redacted)
