"""Verifier crashes versus verification failures (sections 14 and 40).

The invariant under test:

    "the check could not be performed" and "the check was performed and it failed"
    are different facts, and AER must never turn the first into the second.

So a crashing verifier leaves an ``ERROR`` event and **no** verdict at all -- not a
``passed=False`` record. The exception is recorded and then re-raised unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import (
    AER,
    EventType,
    H1CountVerifier,
    HumanVerifier,
    PredicateVerifier,
    RunStateError,
    VerificationContext,
    VerificationInputError,
)


def boom(context: VerificationContext) -> bool:
    del context
    raise RuntimeError("the judge is unavailable")


def context_for(run_id: str) -> VerificationContext:
    return VerificationContext(run_id=run_id)


class TestCrashSemantics:
    def test_the_exception_propagates_unchanged(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError, match="the judge is unavailable"):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

    def test_an_error_event_is_recorded(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        events = aer.get_events(run.run_id)
        assert events[-1].event_type is EventType.ERROR
        assert events[-1].input is not None
        assert events[-1].input["error_type"] == "builtins.RuntimeError"
        assert events[-1].input["recoverable"] is False

    def test_no_fake_failed_verdict_is_created(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        assert aer.get_verifications(run.run_id) == []
        assert aer.verifications.count_by_run(run.run_id) == 0
        # And nothing that looks like a VERIFICATION event either.
        assert EventType.VERIFICATION not in {
            event.event_type for event in aer.get_events(run.run_id)
        }

    def test_the_crash_is_attributed_to_the_verifier(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        errors = aer.get_errors(run.run_id)
        assert len(errors) == 1
        error = errors[0]
        assert error.error_type == "builtins.RuntimeError"
        assert error.error_message == "the judge is unavailable"
        assert error.recoverable is False
        assert error.resolved is False
        assert error.metadata["source"] == "verifier"
        assert error.metadata["verifier"] == "boom"
        assert error.metadata["verifier_type"] == "DETERMINISTIC"
        assert error.stack_trace is not None
        assert "RuntimeError: the judge is unavailable" in error.stack_trace

    def test_the_error_event_is_the_one_the_record_points_at(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        error = aer.get_errors(run.run_id)[0]
        assert error.event_id == aer.get_events(run.run_id)[-1].id

    def test_a_crash_does_not_abort_a_running_task(self, aer: AER) -> None:
        """The agent can carry on: a broken verifier is not a broken task."""
        run = aer.start_run(task="task")

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        with run.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        run.success()

        assert run.status.value == "SUCCESS"
        assert [event.event_type for event in aer.get_events(run.run_id)] == [
            EventType.TASK_START,
            EventType.ERROR,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.TASK_END,
        ]

    def test_a_crash_after_success_does_not_rewrite_history(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        assert [event.event_type for event in aer.get_events(run.run_id)] == [
            EventType.TASK_START,
            EventType.TASK_END,
            EventType.ERROR,
        ]
        # The terminal guard still applies to the agent, even though the runtime
        # just appended its own observation.
        with pytest.raises(RunStateError, match="already finished"):
            run.tool("too_late")

    def test_an_input_error_crashes_the_same_way(self, aer: AER) -> None:
        """A verifier that cannot find its evidence is broken, not disproved."""
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(VerificationInputError, match="actual_count"):
            run.verify(H1CountVerifier(expected_count=1), context=context_for(run.run_id))

        assert aer.get_verifications(run.run_id) == []
        assert aer.get_errors(run.run_id)[0].error_type == ("aer.exceptions.VerificationInputError")

    def test_an_undecided_human_review_crashes_rather_than_rejecting(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(VerificationInputError, match="no recorded human decision"):
            run.verify(HumanVerifier(approved=None), context=context_for(run.run_id))

        assert aer.get_verifications(run.run_id) == []

    def test_repeated_crashes_each_leave_a_trace(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()
        verifier = PredicateVerifier(name="boom", predicate=boom)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                run.verify(verifier, context=context_for(run.run_id))

        assert [event.event_type for event in aer.get_events(run.run_id)] == [
            EventType.TASK_START,
            EventType.TASK_END,
            EventType.ERROR,
            EventType.ERROR,
        ]
        assert len(aer.get_errors(run.run_id)) == 2
        assert aer.get_verifications(run.run_id) == []

    def test_a_crash_and_a_failing_verdict_are_both_representable(self, aer: AER) -> None:
        """The two facts coexist without being confused for one another."""
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )

        verdict = run.verify(
            PredicateVerifier(name="honest", predicate=lambda ctx: False),
            context=context_for(run.run_id),
        )

        assert verdict.passed is False
        assert len(aer.get_verifications(run.run_id)) == 1
        assert len(aer.get_errors(run.run_id)) == 1

    def test_the_crash_survives_a_restart(self, data_dir: Path) -> None:
        first = AER(data_dir)
        run = first.start_run(task="task")
        run.success()
        with pytest.raises(RuntimeError):
            run.verify(
                PredicateVerifier(name="boom", predicate=boom), context=context_for(run.run_id)
            )
        run_id = run.run_id
        first.close()

        second = AER(data_dir)
        try:
            errors = second.get_errors(run_id)
            assert len(errors) == 1
            assert errors[0].metadata["source"] == "verifier"
            assert second.get_verifications(run_id) == []
            assert [event.event_type for event in second.get_events(run_id)][-1] is EventType.ERROR
        finally:
            second.close()
