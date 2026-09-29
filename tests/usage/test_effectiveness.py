"""Effectiveness: joining usage to runs and verdicts, without collapsing the classes
(sections 25-29, 68-70).

Two tests in this file are the milestone's core attribution checks. Both describe the
same shape -- an experience present in a run's context and an outcome -- and both
assert that the system refuses to draw the tempting conclusion from it:

* section 69: injected, **ignored**, and the task still succeeded. The retrieval and
  injection counters move, the adoption counter does not, and the experience is not
  marked helpful;
* section 70: injected, **adopted**, and the task failed. The adoption counter moves
  and the failure is counted -- adoption is not evidence of correctness.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    ExperienceKind,
    RecordNotFoundError,
    UsageSignal,
    UtilityLabel,
)
from tests.knowledge.support import RecordingIndex
from tests.usage.support import (
    agent_failed_run,
    inconclusive_run,
    indexed_for,
    start_run,
    unverified_success_run,
    use_experience,
    verified_failure_run,
    verified_success_run,
)


class TestOutcomeClasses:
    def test_injected_into_a_verified_success(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = verified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.retrieval_count == 1
        assert report.injection_count == 1
        assert report.distinct_target_runs == 1
        assert report.verified_success_runs == 1
        assert report.verified_failure_runs == 0
        assert report.observed_success_rate == 1.0
        assert report.injected_verified_total == 1

    def test_injected_into_a_verified_failure(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = verified_failure_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.verified_failure_runs == 1
        assert report.verified_success_runs == 0
        assert report.observed_success_rate == 0.0
        # An adopted experience that was adopted into a failure is still not a
        # success, whichever way the report is sliced.
        assert report.adopted_verified_success_rate == 0.0

    def test_injected_into_a_run_nobody_described(self, usage_runtime: AER, index) -> None:
        """Round-8.1.1 section 1: unknown is not failure.

        An ``INCONCLUSIVE`` run has no verdict deciding it, so it lands in
        ``UNVERIFIED``. Counting it as ``RUN_FAILED`` would make an experience look
        worse the less its target runs were measured, and would record "nobody said how
        this ended" as "the agent said it failed".
        """
        from aer.usage.effectiveness import classify_outcome

        run_id = inconclusive_run(usage_runtime)
        use_experience(usage_runtime, index, indexed_for(usage_runtime), run_id=run_id)

        run = usage_runtime.get_run(run_id)
        assert classify_outcome(run, usage_runtime.get_verification_summary(run_id)).value == (
            "UNVERIFIED"
        )

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.verified_success_runs == 0
        assert report.verified_failure_runs == 0
        assert report.observed_success_rate is None

    def test_injected_into_a_run_verification_settled(self, usage_runtime: AER, index) -> None:
        """The other half: evidence promotes it to a verified success."""
        from aer.usage.effectiveness import classify_outcome

        run_id = inconclusive_run(usage_runtime, verified=True)
        use_experience(usage_runtime, index, indexed_for(usage_runtime), run_id=run_id)

        run = usage_runtime.get_run(run_id)
        summary = usage_runtime.get_verification_summary(run_id)
        assert classify_outcome(run, summary).value == "VERIFIED_SUCCESS"

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.verified_success_runs == 1
        assert report.observed_success_rate == 1.0

    def test_injected_into_a_run_nobody_verified(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 26: unverified is not failure, so it is not in the denominator."""
        hit = indexed_for(usage_runtime)
        run_id = unverified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.unverified_runs == 1
        assert report.verified_total == 0
        assert report.observed_success_rate is None

    def test_injected_into_a_run_the_agent_declared_failed(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = agent_failed_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.run_failed_runs == 1
        assert report.verified_total == 0
        assert report.observed_success_rate is None

    def test_injected_into_a_run_that_has_not_finished(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = start_run(usage_runtime, task="still going")

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.running_runs == 1
        assert report.observed_success_rate is None

    def test_the_five_classes_are_counted_separately(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """One experience, five runs, five different outcomes -- and five numbers."""
        hit = indexed_for(usage_runtime)
        for run_id in (
            verified_success_run(usage_runtime, run_id="run-success"),
            verified_failure_run(usage_runtime, run_id="run-verified-failure"),
            agent_failed_run(usage_runtime, run_id="run-agent-failure"),
            unverified_success_run(usage_runtime, run_id="run-unverified"),
            start_run(usage_runtime, run_id="run-running"),
        ):
            use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.verified_success_runs == 1
        assert report.verified_failure_runs == 1
        assert report.run_failed_runs == 1
        assert report.unverified_runs == 1
        assert report.running_runs == 1
        assert report.distinct_target_runs == 5
        assert report.outcome_total == 5
        assert report.observed_success_rate == 0.5
        assert report.adopted_verified_success_rate == 0.5


class TestExposureIsNotUse:
    def test_retrieved_without_injection_does_not_reach_the_outcome_metrics(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 2: a result nobody rendered is not exposure."""
        hit = indexed_for(usage_runtime)
        run_id = verified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id, injected=False)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.retrieval_count == 1
        assert report.injection_count == 0
        assert report.never_injected is True
        assert report.distinct_target_runs == 0
        assert report.observed_success_rate is None

    def test_a_retrieval_with_no_run_is_reported_as_unattributed(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 24: a session recorded before its run is honest about it."""
        hit = indexed_for(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=None)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.retrieval_count == 1
        assert report.injection_count == 1
        assert report.unattributed_usage_count == 1
        assert report.distinct_target_runs == 0
        assert report.observed_success_rate is None

    def test_counts_are_distinct_runs_not_rows(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Ten retrievals inside one run are one run's worth of evidence.

        This is what stops a retry loop from making an experience look proven.
        """
        hit = indexed_for(usage_runtime)
        run_id = verified_success_run(usage_runtime)

        for _ in range(3):
            use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.retrieval_count == 3
        assert report.injection_count == 3
        assert report.explicit_adoption_count == 3
        assert report.distinct_target_runs == 1
        assert report.verified_success_runs == 1
        assert report.adopted_runs == 1


class TestCriticalAttributionIgnoredButSuccessful:
    """Section 69: the test this milestone exists to pass."""

    def test_an_ignored_experience_gets_no_credit_for_the_success(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = verified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id, value=UsageSignal.IGNORED)

        report = usage_runtime.experience_effectiveness("exp-1")
        # The exposure counters moved...
        assert report.retrieval_count == 1
        assert report.injection_count == 1
        assert report.explicit_ignore_count == 1
        # ...and the attribution counters did not.
        assert report.explicit_adoption_count == 0
        assert report.adopted_runs == 0
        assert report.helpful_count == 0
        assert report.adopted_verified_success_rate is None
        # The run did succeed, and the report says so about the *run*, not about the
        # experience having caused it.
        assert report.verified_success_runs == 1
        assert report.observed_success_rate == 1.0

    def test_a_successful_run_does_not_make_anything_helpful(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 14: the agent solved it alone. Nothing may write HELPFUL."""
        hit = indexed_for(usage_runtime)
        run_id = verified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id, value=None)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.helpful_count == 0
        assert report.neutral_count == 0
        assert report.harmful_count == 0
        assert report.explicit_adoption_count == 0


class TestCriticalAttributionAdoptedButFailed:
    """Section 70: adoption is a claim about the agent, not about the world."""

    def test_an_adopted_experience_in_a_failed_run_is_counted_as_a_failure(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        run_id = verified_failure_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id)

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.explicit_adoption_count == 1
        assert report.adopted_runs == 1
        assert report.adopted_verified_success_runs == 0
        assert report.adopted_verified_failure_runs == 1
        assert report.verified_failure_runs == 1
        assert report.adopted_verified_success_rate == 0.0


class TestUtilityFeedback:
    def test_labels_are_tallied_by_class(self, usage_runtime: AER, index: RecordingIndex) -> None:
        hit = indexed_for(usage_runtime)
        for run_id, label in (
            ("run-a", UtilityLabel.HELPFUL),
            ("run-b", UtilityLabel.HELPFUL),
            ("run-c", UtilityLabel.NEUTRAL),
            ("run-d", UtilityLabel.HARMFUL),
        ):
            use_experience(
                usage_runtime,
                index,
                hit,
                run_id=verified_success_run(usage_runtime, run_id=run_id),
                utility_value=label,
            )

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.helpful_count == 2
        assert report.neutral_count == 1
        assert report.harmful_count == 1
        # Utility is independent of adoption: all four were adopted too.
        assert report.explicit_adoption_count == 4


class TestSeveralExperiences:
    def test_each_experience_gets_its_own_report(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        first = indexed_for(usage_runtime, experience_id="exp-1")
        second = indexed_for(usage_runtime, experience_id="exp-2")

        use_experience(
            usage_runtime,
            index,
            first,
            run_id=verified_success_run(usage_runtime, run_id="run-1"),
        )
        use_experience(
            usage_runtime,
            index,
            second,
            run_id=verified_failure_run(usage_runtime, run_id="run-2"),
        )

        reports = usage_runtime.experience_effectiveness_all()
        by_id = {report.experience_id: report for report in reports}
        assert by_id["exp-1"].observed_success_rate == 1.0
        assert by_id["exp-2"].observed_success_rate == 0.0
        assert usage_runtime.effectiveness.report("exp-2").verified_failure_runs == 1

    def test_batch_reports_come_back_in_the_order_asked_for(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime),
        )

        reports = usage_runtime.effectiveness.reports(["exp-1"])

        assert [report.experience_id for report in reports] == ["exp-1"]


class TestBoundaries:
    def test_an_unknown_experience_raises(self, usage_runtime: AER) -> None:
        """Zeroes are a real state, so a missing experience must not look like one."""
        with pytest.raises(RecordNotFoundError):
            usage_runtime.experience_effectiveness("no-such-experience")

    def test_an_experience_with_no_usage_reports_zeroes(self, usage_runtime: AER) -> None:
        from tests.usage.support import store_experience

        store_experience(usage_runtime, experience_id="exp-quiet")

        report = usage_runtime.experience_effectiveness("exp-quiet")
        assert report.retrieval_count == 0
        assert report.has_evidence is False
        assert report.observed_success_rate is None
        assert report.adopted_verified_success_rate is None
        assert "n/a" in report.describe()

    def test_a_failure_experience_is_reported_like_any_other(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Warnings are injected too, and their usage counts the same way."""
        hit = indexed_for(
            usage_runtime,
            experience_id="exp-bad",
            kind=ExperienceKind.FAILURE,
            outcome_verified=True,
        )
        run_id = verified_success_run(usage_runtime)

        use_experience(usage_runtime, index, hit, run_id=run_id, value=UsageSignal.IGNORED)

        report = usage_runtime.experience_effectiveness("exp-bad")
        assert report.injection_count == 1
        assert report.explicit_ignore_count == 1
        assert report.verified_success_runs == 1
