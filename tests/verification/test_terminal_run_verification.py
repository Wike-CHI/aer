"""Terminal-run rules around verification (Milestone 4, sections 31-33, 41).

The distinction this file protects:

* **agent mutation** -- ``emit``, ``tool``, ``error``, ``recovery`` -- describes
  what the agent did. Once the run is terminal the agent is done, so these raise.
* **system observation** -- ``VERIFICATION`` (and human feedback) -- describes what
  the *system* later found out about the run. A production deployment verifies
  after the agent finishes, so these are allowed.

The guard was not deleted or loosened; a whitelist was added next to it.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    EventType,
    H1CountVerifier,
    HttpStatusVerifier,
    RunStateError,
    VerificationContext,
)


def context_for(run_id: str, **payload: object) -> VerificationContext:
    return VerificationContext(run_id=run_id, payload=payload)  # type: ignore[arg-type]


class TestAgentMutationsAreStillBlocked:
    def test_emit_is_blocked(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RunStateError, match="cannot emit MODEL_CALL"):
            run.emit(EventType.MODEL_CALL)

    def test_tool_is_blocked(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RunStateError, match="cannot open a tool hook"):
            run.tool("wordpress.update_page")

    def test_error_is_blocked(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RunStateError, match="cannot emit ERROR"):
            run.error(RuntimeError("too late"))

    def test_recovery_is_blocked(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        with pytest.raises(RunStateError, match="cannot open a recovery hook"):
            run.recovery(reason="too late")

    def test_every_terminal_status_blocks_mutations(self, aer: AER) -> None:
        for finish in ("success", "partial_success", "fail", "abort"):
            run = aer.start_run(task=f"task-{finish}")
            getattr(run, finish)()

            with pytest.raises(RunStateError, match="already finished"):
                run.emit(EventType.MODEL_CALL)

    def test_a_blocked_mutation_writes_nothing(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()
        before = aer.get_events(run.run_id)

        with pytest.raises(RunStateError):
            run.emit(EventType.MODEL_CALL)

        assert aer.get_events(run.run_id) == before
        assert aer.get_errors(run.run_id) == []


class TestSystemObservationsAreAllowed:
    def test_verify_is_allowed_after_success(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()

        record = run.verify(
            HttpStatusVerifier(expected_status=200),
            context=context_for(run.run_id, actual_status=200),
        )

        assert record.passed is True
        assert aer.verifications.count_by_run(run.run_id) == 1

    def test_verify_is_allowed_after_failure(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.fail()

        record = run.verify(
            HttpStatusVerifier(expected_status=200),
            context=context_for(run.run_id, actual_status=200),
        )

        assert record.passed is True
        stored = aer.get_run(run.run_id)
        assert stored is not None
        assert stored.status.value == "FAILED"

    def test_verify_is_allowed_after_abort(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.abort()

        assert run.verify(
            HttpStatusVerifier(expected_status=200),
            context=context_for(run.run_id, actual_status=200),
        ).passed

    def test_the_observation_continues_the_sequence(self, aer: AER) -> None:
        run = aer.start_run(task="task")
        run.success()
        run.verify(
            HttpStatusVerifier(expected_status=200),
            context=context_for(run.run_id, actual_status=200),
        )
        run.verify(
            HttpStatusVerifier(expected_status=500, name="http_500"),
            context=context_for(run.run_id, actual_status=200),
        )

        events = aer.get_events(run.run_id)
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TASK_END,
            EventType.VERIFICATION,
            EventType.VERIFICATION,
        ]

    def test_human_feedback_is_an_allowed_system_observation(self, aer: AER) -> None:
        """The other whitelist member, reachable only through the internal path."""
        run = aer.start_run(task="task")
        run.success()

        event = run._append_system_event(
            EventType.HUMAN_FEEDBACK,
            output={"approved": True, "source": "support-ticket"},
        )

        assert event.event_type is EventType.HUMAN_FEEDBACK
        assert event.sequence == 3
        assert aer.get_events(run.run_id)[-1].output == {
            "approved": True,
            "source": "support-ticket",
        }

    def test_the_whitelist_rejects_agent_events(self, aer: AER) -> None:
        """``_append_system_event`` is not a general bypass of the terminal guard."""
        run = aer.start_run(task="task")
        run.success()

        for event_type in (
            EventType.TASK_START,
            EventType.MODEL_CALL,
            EventType.MODEL_RESULT,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.RECOVERY_START,
            EventType.RECOVERY_RESULT,
            EventType.TASK_END,
        ):
            with pytest.raises(RunStateError, match="is not a system observation event"):
                run._append_system_event(event_type)

        assert len(aer.get_events(run.run_id)) == 2

    def test_verification_does_not_consume_the_run(self, aer: AER) -> None:
        """Observing a finished run must not un-finish it or finish it twice."""
        run = aer.start_run(task="task")
        run.success()

        run.verify(
            H1CountVerifier(expected_count=1),
            context=context_for(run.run_id, actual_count=0),
        )

        assert run.is_finished is True
        with pytest.raises(RunStateError, match="already finished"):
            run.success()

        stored = aer.get_run(run.run_id)
        assert stored is not None
        assert stored.status.value == "SUCCESS"
        assert stored.ended_at is not None
