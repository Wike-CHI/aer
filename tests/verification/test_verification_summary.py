"""Run-level aggregation and the derived ``verified_success`` (sections 23-25, 42).

``verified_success`` is where the whole milestone cashes out, so it is tested
exhaustively as a pure function first -- every combination of run status, required
and optional verdicts -- and then once end to end against a real runtime.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    EventType,
    H1CountVerifier,
    HttpStatusVerifier,
    RecordNotFoundError,
    RunStatus,
    VerificationContext,
    VerificationRecord,
    VerificationSummary,
    VerifierType,
    is_verified_success,
)


def verdict(
    *,
    passed: bool,
    required: bool = True,
    name: str = "check",
) -> VerificationRecord:
    return VerificationRecord(
        run_id="run-1",
        verifier_type=VerifierType.DETERMINISTIC,
        verifier_name=name,
        passed=passed,
        required=required,
    )


def summary_of(*verdicts: VerificationRecord) -> VerificationSummary:
    return VerificationSummary.from_records("run-1", list(verdicts))


class TestSummaryArithmetic:
    def test_no_verifications_is_not_a_pass(self) -> None:
        """Section 23: "nobody checked" must never be reported as "all good"."""
        summary = summary_of()

        assert summary.total == 0
        assert summary.passed == 0
        assert summary.failed == 0
        assert summary.pass_rate is None
        assert summary.all_passed is False
        assert summary.all_required_passed is False

    def test_a_single_pass(self) -> None:
        summary = summary_of(verdict(passed=True))

        assert (summary.total, summary.passed, summary.failed) == (1, 1, 0)
        assert summary.pass_rate == 1.0
        assert summary.all_passed is True

    def test_a_single_failure(self) -> None:
        summary = summary_of(verdict(passed=False))

        assert (summary.total, summary.passed, summary.failed) == (1, 0, 1)
        assert summary.pass_rate == 0.0
        assert summary.all_passed is False

    def test_two_passes(self) -> None:
        summary = summary_of(verdict(passed=True), verdict(passed=True, name="second"))

        assert summary.total == 2
        assert summary.pass_rate == 1.0
        assert summary.all_passed is True

    def test_one_pass_and_one_failure(self) -> None:
        summary = summary_of(verdict(passed=True), verdict(passed=False, name="second"))

        assert summary.total == 2
        assert summary.pass_rate == 0.5
        assert summary.all_passed is False

    def test_required_and_optional_are_counted_separately(self) -> None:
        summary = summary_of(
            verdict(passed=True),
            verdict(passed=True, required=False, name="llm"),
            verdict(passed=False, required=False, name="judge"),
        )

        assert summary.total == 3
        assert summary.failed == 1
        assert summary.required_total == 1
        assert summary.required_passed == 1
        assert summary.required_failed == 0


class TestVerifiedSuccess:
    def test_a_successful_run_with_a_passing_required_check(self) -> None:
        assert is_verified_success(RunStatus.SUCCESS, summary_of(verdict(passed=True))) is True

    def test_agent_says_success_verifier_says_failed(self) -> None:
        """The core case of this milestone (section 26)."""
        summary = summary_of(verdict(passed=False))

        assert is_verified_success(RunStatus.SUCCESS, summary) is False

    def test_success_without_any_verification_is_not_a_verified_success(self) -> None:
        assert is_verified_success(RunStatus.SUCCESS, summary_of()) is False

    def test_a_failed_run_is_never_a_verified_success(self) -> None:
        """Section 28: passing checks show the world is fine, not that the agent finished."""
        for status in (RunStatus.FAILED, RunStatus.ABORTED, RunStatus.PARTIAL_SUCCESS):
            assert is_verified_success(status, summary_of(verdict(passed=True))) is False

    def test_a_running_run_is_not_a_verified_success(self) -> None:
        assert is_verified_success(RunStatus.RUNNING, summary_of(verdict(passed=True))) is False

    def test_one_failed_required_check_blocks_it(self) -> None:
        summary = summary_of(
            verdict(passed=True, name="http"),
            verdict(passed=False, name="h1"),
            verdict(passed=True, name="schema"),
        )

        assert is_verified_success(RunStatus.SUCCESS, summary) is False

    def test_an_optional_failure_does_not_block_it(self) -> None:
        """Section 25: an opinion must not veto a proven task."""
        summary = summary_of(
            verdict(passed=True, name="http"),
            verdict(passed=False, required=False, name="llm_quality"),
        )

        assert summary.all_passed is False
        assert summary.all_required_passed is True
        assert is_verified_success(RunStatus.SUCCESS, summary) is True

    def test_only_optional_verdicts_do_not_count_as_verified(self) -> None:
        """An optional check is not evidence that a required one was done."""
        summary = summary_of(verdict(passed=True, required=False, name="llm"))

        assert summary.all_required_passed is False
        assert is_verified_success(RunStatus.SUCCESS, summary) is False


class TestSummaryEndToEnd:
    @staticmethod
    def context(run_id: str) -> VerificationContext:
        return VerificationContext(run_id=run_id, payload={"actual_status": 200, "actual_count": 0})

    def test_a_run_with_no_verdicts_is_not_all_passed(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        summary = run.get_verification_summary()

        assert summary.total == 0
        assert summary.all_passed is False
        assert run.verified_success() is False

    def test_agent_success_with_a_failing_verifier_end_to_end(self, aer: AER) -> None:
        run = aer.start_run(task="Fix the H1", task_type="wordpress")
        with run.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        run.success()

        record = run.verify(H1CountVerifier(expected_count=1), context=self.context(run.run_id))
        summary = run.get_verification_summary()

        assert record.passed is False
        assert summary.total == 1
        assert summary.failed == 1
        assert summary.pass_rate == 0.0
        assert run.verified_success() is False
        assert aer.verified_success(run.run_id) is False

        stored = aer.get_run(run.run_id)
        assert stored is not None
        assert stored.status is RunStatus.SUCCESS

    def test_agent_success_with_passing_verifiers_end_to_end(self, aer: AER) -> None:
        run = aer.start_run(task="Fix the H1", task_type="wordpress")
        context = VerificationContext(
            run_id=run.run_id, payload={"actual_status": 200, "actual_count": 1}
        )
        run.success()

        run.verify(HttpStatusVerifier(expected_status=200), context=context)
        run.verify(H1CountVerifier(expected_count=1), context=context)

        summary = run.get_verification_summary()
        assert summary.total == 2
        assert summary.all_passed is True
        assert summary.pass_rate == 1.0
        assert run.verified_success() is True

    def test_verifying_a_failed_run_does_not_rehabilitate_it(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.fail()

        run.verify(HttpStatusVerifier(expected_status=200), context=self.context(run.run_id))

        assert run.get_verification_summary().all_passed is True
        assert run.verified_success() is False
        stored = aer.get_run(run.run_id)
        assert stored is not None
        assert stored.status is RunStatus.FAILED

    def test_summary_is_recomputed_not_cached(self, aer: AER) -> None:
        """A verdict recorded later must show up without reopening anything."""
        run = aer.start_run(task="task")
        run.success()
        assert run.verified_success() is False

        run.verify(HttpStatusVerifier(expected_status=200), context=self.context(run.run_id))

        assert run.verified_success() is True
        assert len(aer.get_events(run.run_id)) == 3

    def test_summarising_an_unknown_run_raises(self, aer: AER) -> None:
        with pytest.raises(RecordNotFoundError, match="Run not found"):
            aer.get_verification_summary("no-such-run")

    def test_verdicts_are_scoped_per_run(self, aer: AER) -> None:
        first = aer.start_run(task="first")
        second = aer.start_run(task="second")
        first.success()
        second.success()

        first.verify(HttpStatusVerifier(expected_status=200), context=self.context(first.run_id))

        assert first.get_verification_summary().total == 1
        assert second.get_verification_summary().total == 0
        assert first.verified_success() is True
        assert second.verified_success() is False

    def test_the_summary_is_serialisable_json(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()
        run.verify(H1CountVerifier(expected_count=1), context=self.context(run.run_id))

        payload = run.get_verification_summary().model_dump(mode="json")

        assert payload["run_id"] == run.run_id
        assert payload["total"] == 1
        assert payload["all_passed"] is False
        assert isinstance(payload["pass_rate"], float)

    def test_a_verdict_cannot_be_recorded_without_a_verification_event(self, aer: AER) -> None:
        """Trace integrity: the pipeline writes both, so a lone record cannot occur."""
        run = aer.start_run(task="task")
        run.success()
        run.verify(HttpStatusVerifier(expected_status=200), context=self.context(run.run_id))

        events = [e for e in aer.get_events(run.run_id) if e.event_type is EventType.VERIFICATION]
        assert len(events) == len(aer.get_verifications(run.run_id)) == 1
