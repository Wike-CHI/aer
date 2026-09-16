"""RunEvidence and its builder (Milestone 5, sections 21-22 and 49).

The evidence package is what a distillation pass is allowed to know. These tests pin
that it is complete, correctly ordered, immutable, and -- critically -- scoped to one
run, because leaking another run's errors into a claim would produce knowledge about
something that never happened.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aer import AER, EventType, RecordNotFoundError, RunEvidence, RunStatus
from tests.experience.support import (
    add_human_feedback,
    build_false_success_run,
    build_plain_success_run,
    build_recovery_run,
    build_unresolved_failure_run,
    context_for,
    http_verifier,
)

#: The full RECOVERY trajectory, in order (round-5 brief section 53).
RECOVERY_TRACE = [
    EventType.TASK_START,
    EventType.TOOL_CALL,
    EventType.ERROR,
    EventType.TOOL_RESULT,
    EventType.RECOVERY_START,
    EventType.RECOVERY_RESULT,
    EventType.TOOL_CALL,
    EventType.TOOL_RESULT,
    EventType.TASK_END,
    EventType.VERIFICATION,
    EventType.VERIFICATION,
]


class TestAggregation:
    def test_every_repository_is_included(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.run.id == run_id
        assert len(evidence.events) == len(RECOVERY_TRACE)
        assert len(evidence.errors) == 1
        assert len(evidence.recoveries) == 1
        assert len(evidence.verifications) == 2

    def test_event_order_is_preserved(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert [event.sequence for event in evidence.events] == list(
            range(1, len(RECOVERY_TRACE) + 1)
        )
        assert [event.event_type for event in evidence.events] == RECOVERY_TRACE

    def test_verdicts_keep_their_order(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert [verdict.verifier_name for verdict in evidence.verifications] == [
            "http_status",
            "h1_count",
        ]


class TestDerivedViews:
    def test_a_recovered_verified_run(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.status is RunStatus.SUCCESS
        assert evidence.is_finished is True
        assert evidence.verified_success is True
        assert evidence.required_failed == 0
        assert evidence.has_errors is True
        assert evidence.has_successful_recovery is True
        assert len(evidence.successful_recoveries) == 1
        assert evidence.failed_recoveries == ()

    def test_an_unresolved_failure(self, aer: AER) -> None:
        run_id = build_unresolved_failure_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.status is RunStatus.FAILED
        assert evidence.verified_success is False
        assert evidence.has_errors is True
        assert evidence.has_successful_recovery is False
        assert len(evidence.failed_recoveries) == 1
        assert evidence.failed_recoveries[0].reason == "clear the cache"

    def test_a_false_success(self, aer: AER) -> None:
        run_id = build_false_success_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.status is RunStatus.SUCCESS
        assert evidence.verified_success is False
        assert evidence.required_failed == 1

    def test_error_messages_are_rendered_from_the_records(self, aer: AER) -> None:
        run_id = build_unresolved_failure_run(aer)

        evidence = aer.get_run_evidence(run_id)

        # Both failures are in the trace: the original one and the repair attempt
        # that itself raised. A failed recovery is evidence too.
        assert evidence.error_messages == (
            "builtins.PermissionError: 403 Forbidden",
            "builtins.RuntimeError: still 403 after clearing the cache",
        )

    def test_tool_names_keep_repeats(self, aer: AER) -> None:
        """The repeated call *is* the retry; losing it would lose the trajectory."""
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.tool_names == ("wordpress.update_page", "wordpress.update_page")

    def test_human_feedback_is_read_from_the_trace(self, aer: AER) -> None:
        run_id = build_plain_success_run(aer)
        assert aer.get_run_evidence(run_id).has_human_feedback is False

        add_human_feedback(aer, run_id, approved=False)

        evidence = aer.get_run_evidence(run_id)
        assert evidence.has_human_feedback is True
        assert len(evidence.human_feedback_events) == 1
        assert evidence.human_feedback_events[0].output == {
            "approved": False,
            "reviewer": "ops-42",
        }

    def test_repr_is_informative(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert "RunEvidence(" in repr(evidence)
        assert run_id in repr(evidence)


class TestIsolation:
    def test_evidence_never_crosses_runs(self, aer: AER) -> None:
        first = build_recovery_run(aer, task="first")
        second = build_unresolved_failure_run(aer, task="second")

        first_evidence = aer.get_run_evidence(first)
        second_evidence = aer.get_run_evidence(second)

        assert {event.run_id for event in first_evidence.events} == {first}
        assert {event.run_id for event in second_evidence.events} == {second}
        assert {error.run_id for error in first_evidence.errors} == {first}
        assert {error.run_id for error in second_evidence.errors} == {second}
        assert {recovery.run_id for recovery in second_evidence.recoveries} == {second}
        assert first_evidence.verified_success is True
        assert second_evidence.verified_success is False

    def test_a_brand_new_run_has_an_empty_package(self, aer: AER) -> None:
        context = aer.start_run(task="nothing happened yet")

        evidence = aer.get_run_evidence(context.run_id)

        assert len(evidence.events) == 1  # just TASK_START
        assert evidence.errors == ()
        assert evidence.recoveries == ()
        assert evidence.verifications == ()
        assert evidence.verified_success is False


class TestBuilderContract:
    def test_an_unknown_run_raises(self, aer: AER) -> None:
        """Silently returning an empty package would let a typo look like a run with
        no evidence, and distilling that would produce knowledge about nothing."""
        with pytest.raises(RecordNotFoundError, match="Run not found"):
            aer.get_run_evidence("no-such-run")

    def test_from_run_reuses_an_already_loaded_run(self, aer: AER) -> None:
        run_id = build_plain_success_run(aer)
        run = aer.get_run(run_id)
        assert run is not None

        evidence = aer.experience_service.evidence.from_run(run)

        assert evidence.run is not None
        assert evidence.run.id == run_id

    def test_the_package_is_frozen(self, aer: AER) -> None:
        run_id = build_plain_success_run(aer)
        evidence = aer.get_run_evidence(run_id)

        with pytest.raises(ValidationError):
            evidence.errors = ()  # type: ignore[misc]

    def test_verification_summary_matches_the_run_level_one(self, aer: AER) -> None:
        """The experience layer reuses Milestone 4's judgement, never restates it."""
        run_id = build_recovery_run(aer)

        evidence = aer.get_run_evidence(run_id)

        assert evidence.verification_summary == aer.get_verification_summary(run_id)

    def test_optional_verifications_do_not_decide_verified_success(self, aer: AER) -> None:
        context = aer.start_run(task="optional only", task_type="wordpress")
        with context.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        context.success()
        context.verify(
            http_verifier(required=False),
            context=context_for(context.run_id, actual_status=500),
        )

        evidence = aer.get_run_evidence(context.run_id)

        assert evidence.verification_summary.failed == 1
        assert evidence.verified_success is False

    def test_the_builder_returns_a_run_evidence_instance(self, aer: AER) -> None:
        run_id = build_plain_success_run(aer)

        assert isinstance(aer.get_run_evidence(run_id), RunEvidence)

    def test_reading_evidence_is_a_pure_read(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)
        before = len(aer.get_events(run_id))

        aer.get_run_evidence(run_id)

        assert len(aer.get_events(run_id)) == before
        assert aer.experiences.count() == 0
