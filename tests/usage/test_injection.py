"""Injection: recording that an experience really reached the agent (sections 18-20, 52, 65).

The distinction this file defends is the milestone's central one. A retrieval result is
a list of candidates; a prompt is what the agent read. Everything here is about the
gap between the two being recorded rather than assumed.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    ExperienceKind,
    RecordNotFoundError,
    UsageTrackingError,
)
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.usage.fingerprints import context_fingerprint
from tests.knowledge.support import RecordingIndex
from tests.usage.support import indexed_for, inject, retrieve


class TestValidInjection:
    def test_a_hit_can_be_recorded_as_injected(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        (row,) = inject(usage_runtime, tracked)

        assert row.is_injected is True
        assert row.injected_at is not None
        assert row.injection_position == 1
        assert row.injection_chars is not None
        assert row.injection_chars > 0
        assert row.formatter_version == FORMATTER_VERSION
        # And it survives the read path unchanged.
        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.is_injected is True

    def test_the_context_fingerprint_is_stored_but_not_the_context(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 19: a digest proves two captures match and leaks nothing."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])
        formatter = ExperienceContextFormatter()
        digest = context_fingerprint(formatter.format(tracked.result))

        (row,) = inject(usage_runtime, tracked, formatter=formatter)

        assert row.context_fingerprint == digest
        assert row.context_fingerprint is not None
        assert len(row.context_fingerprint) == 64
        # The rendered text itself is nowhere in the row.
        assert "Historical" not in str(row.model_dump())

    def test_retrieval_without_injection_leaves_the_row_untouched(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        (row,) = usage_runtime.get_session_usage(tracked.session_id)
        assert row.is_injected is False
        assert row.context_fingerprint is None


class TestPartialInjection:
    def test_only_part_of_a_result_may_be_injected(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 20: a caller that renders two of three records records two."""
        hits = [
            indexed_for(usage_runtime, experience_id=f"exp-{position}") for position in (1, 2, 3)
        ]
        tracked = retrieve(usage_runtime, index, guidance=hits)

        rows = inject(usage_runtime, tracked, experience_ids=["exp-1", "exp-3"])

        assert [row.experience_id for row in rows] == ["exp-1", "exp-3"]
        assert [row.injection_position for row in rows] == [1, 3]
        skipped = usage_runtime.get_experience_usage(tracked.session_id, "exp-2")
        assert skipped is not None
        assert skipped.is_injected is False
        assert (
            usage_runtime.experience_usage.count(session_id=tracked.session_id, injected_only=True)
            == 2
        )


class TestInjectionValidation:
    def test_an_experience_outside_the_result_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 20: a usage row for something nobody retrieved is a fabrication.

        Written against ``record_injection`` directly rather than through the test
        helper: the helper intersects the requested ids with the result, which is what
        a sane caller does, and this test is precisely about a caller that does not.
        """
        indexed_for(usage_runtime, experience_id="exp-other")
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        with pytest.raises(UsageTrackingError, match="not part of retrieval session"):
            usage_runtime.record_injection(
                session_id=tracked.session_id, experience_ids=["exp-other"]
            )

        assert usage_runtime.experience_usage.count(injected_only=True) == 0

    def test_an_unknown_session_is_refused(self, usage_runtime: AER) -> None:
        with pytest.raises(RecordNotFoundError):
            usage_runtime.record_injection(session_id="no-such-session", experience_ids=["exp-1"])

    def test_an_empty_list_is_refused(self, usage_runtime: AER, index: RecordingIndex) -> None:
        """Indistinguishable from a call that forgot to pass one, so it is an error."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        with pytest.raises(UsageTrackingError, match="at least one"):
            usage_runtime.record_injection(session_id=tracked.session_id, experience_ids=[])

    def test_the_same_experience_twice_in_one_call_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Again bypassing the helper, which de-duplicates for the caller."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        with pytest.raises(UsageTrackingError, match="more than once"):
            usage_runtime.record_injection(
                session_id=tracked.session_id, experience_ids=["exp-1", "exp-1"]
            )

    def test_detail_for_an_unrelated_experience_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """A stray key would otherwise be dropped, and a dropped size reads as
        "injected with no measured size"."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        with pytest.raises(UsageTrackingError, match="positions names"):
            usage_runtime.record_injection(
                session_id=tracked.session_id,
                experience_ids=["exp-1"],
                positions={"exp-somewhere-else": 1},
            )


class TestInjectionIsIdempotent:
    def test_recording_the_same_injection_twice_changes_nothing(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 52: a context rendered twice is still one injection, so the
        original timestamp and fingerprint stand."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])

        first = inject(usage_runtime, tracked)
        second = inject(usage_runtime, tracked)

        assert first[0].injected_at == second[0].injected_at
        assert first[0].context_fingerprint == second[0].context_fingerprint
        assert usage_runtime.experience_usage.count(injected_only=True) == 1

    def test_a_contradicting_second_injection_is_refused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """A different context fingerprint for the same experience in the same
        session is not a repeat, it is a contradiction."""
        tracked = retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])
        inject(usage_runtime, tracked)

        with pytest.raises(UsageTrackingError, match="different context fingerprint"):
            usage_runtime.record_injection(
                session_id=tracked.session_id,
                experience_ids=["exp-1"],
                context_fingerprint=context_fingerprint("a different rendering"),
            )

        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.context_fingerprint == context_fingerprint(
            ExperienceContextFormatter().format(tracked.result)
        )


class TestMixedResultInjection:
    def test_guidance_and_warnings_can_be_injected_together(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        guidance = indexed_for(usage_runtime, experience_id="exp-good")
        failure = indexed_for(
            usage_runtime,
            experience_id="exp-bad",
            kind=ExperienceKind.FAILURE,
            outcome_verified=True,
        )
        tracked = retrieve(usage_runtime, index, guidance=[guidance], failures=[failure])

        rows = inject(usage_runtime, tracked)

        assert [row.experience_id for row in rows] == ["exp-good", "exp-bad"]
        assert [row.role.value for row in rows] == ["GUIDANCE", "WARNING"]
        assert all(row.is_injected for row in rows)
