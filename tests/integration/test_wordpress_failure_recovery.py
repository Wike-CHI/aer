"""Milestone 3 acceptance: a complete failure-and-recovery trace.

Implements sections 31 (persistence) and 37 (final acceptance) of the round-3
brief. The scenario is the first production shape AER has to survive:

.. code-block:: text

    Run Start
      -> TOOL_CALL wordpress.update_page
      -> PermissionError("403 Forbidden")
      -> ERROR
      -> TOOL_RESULT (failed)
      -> RECOVERY_START
      -> [permission fix]
      -> RECOVERY_RESULT (success)
      -> TOOL_CALL wordpress.update_page
      -> TOOL_RESULT (success)
      -> Run SUCCESS
      -> TASK_END

The same assertions are then re-run against a freshly opened database, because a
trace nobody can read back is not a trace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import AER, EventType, RunStatus

TOOL = "wordpress.update_page"
PAYLOAD = {"page_id": 123, "h1": "New H1"}

EXPECTED_SEQUENCE = [
    EventType.TASK_START,
    EventType.TOOL_CALL,
    EventType.ERROR,
    EventType.TOOL_RESULT,
    EventType.RECOVERY_START,
    EventType.RECOVERY_RESULT,
    EventType.TOOL_CALL,
    EventType.TOOL_RESULT,
    EventType.TASK_END,
]


def run_scenario(aer: AER) -> tuple[str, str]:
    """Execute the scenario and return ``(run_id, error_id)``."""
    context = aer.start_run(
        task="Fix WordPress product page H1",
        task_type="wordpress",
        agent_name="wp-agent",
        agent_version="0.1.0",
    )

    # 1. First attempt: the REST API rejects the call.
    first_attempt = context.tool(TOOL, input=PAYLOAD)
    with pytest.raises(PermissionError, match="403 Forbidden"), first_attempt:
        raise PermissionError("403 Forbidden")

    error_record = first_attempt.error_record
    assert error_record is not None, "the tool hook must record its own failure"

    # 2. Diagnose, repair, and link the repair to the recorded failure.
    with context.recovery(
        reason="REST API returned 403",
        error_id=error_record.id,
    ) as recovery:
        recovery.set_result({"action": "granted_edit_posts", "verified": True})

    assert recovery.record is not None
    assert recovery.record.success is True

    # 3. Retry: now it works.
    with context.tool(TOOL, input=PAYLOAD) as second_attempt:
        second_attempt.set_result({"status": 200, "h1": "New H1"})

    context.success(final_score=1.0)
    return context.run_id, error_record.id


def assert_trace_is_intact(aer: AER, run_id: str, error_id: str) -> None:
    """Every assertion the acceptance scenario makes, against a given runtime."""
    run = aer.get_run(run_id)
    assert run is not None
    assert run.status is RunStatus.SUCCESS
    assert run.task_type == "wordpress"
    assert run.final_score == 1.0
    assert run.ended_at is not None

    events = aer.get_events(run_id)
    assert [event.sequence for event in events] == list(range(1, 10))
    assert [event.event_type for event in events] == EXPECTED_SEQUENCE

    by_type = {event.event_type: event for event in events}

    # The failed attempt: call, error, result -- in that order.
    assert events[1].input == {"tool": TOOL, "arguments": PAYLOAD}
    assert events[3].output["success"] is False
    assert events[3].output["error"]["error_id"] == error_id
    assert events[3].duration_ms is not None

    # The repair.
    assert by_type[EventType.RECOVERY_START].input == {
        "reason": "REST API returned 403",
        "error_id": error_id,
    }
    assert by_type[EventType.RECOVERY_RESULT].output["success"] is True
    assert by_type[EventType.RECOVERY_RESULT].output["result"] == {
        "action": "granted_edit_posts",
        "verified": True,
    }

    # The retry.
    assert events[6].input == {"tool": TOOL, "arguments": PAYLOAD}
    assert events[7].output["success"] is True
    assert events[7].output["result"] == {"status": 200, "h1": "New H1"}

    # The structured failure record.
    errors = aer.get_errors(run_id)
    assert len(errors) == 1
    error = errors[0]
    assert error.id == error_id
    assert error.error_type == "builtins.PermissionError"
    assert error.error_message == "403 Forbidden"
    assert error.stack_trace is not None
    assert "PermissionError: 403 Forbidden" in error.stack_trace
    assert error.recoverable is True
    assert error.resolved is True
    assert error.event_id == events[2].id
    assert error.metadata == {"hook": "tool", "tool": TOOL}

    # The repair record.
    recoveries = aer.get_recoveries(run_id)
    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery.error_id == error_id
    assert recovery.success is True
    assert recovery.duration_ms is not None
    assert recovery.outcome == {"action": "granted_edit_posts", "verified": True}
    assert recovery.start_event_id == by_type[EventType.RECOVERY_START].id
    assert recovery.result_event_id == by_type[EventType.RECOVERY_RESULT].id


def test_the_acceptance_scenario_produces_a_complete_trace(aer: AER) -> None:
    run_id, error_id = run_scenario(aer)

    assert_trace_is_intact(aer, run_id, error_id)


def test_the_complete_trace_survives_a_restart(data_dir: Path) -> None:
    """Section 31: close the database, reopen it, and re-read the same story."""
    first = AER(data_dir)
    run_id, error_id = run_scenario(first)
    first.close()
    assert first.is_closed is True

    second = AER(data_dir)
    try:
        assert_trace_is_intact(second, run_id, error_id)
    finally:
        second.close()


def test_the_scenario_is_repeatable(aer: AER) -> None:
    """Two full runs must not share events, errors or recoveries."""
    first_run, first_error = run_scenario(aer)
    second_run, second_error = run_scenario(aer)

    assert first_run != second_run
    assert first_error != second_error

    assert [event.sequence for event in aer.get_events(first_run)] == list(range(1, 10))
    assert [event.sequence for event in aer.get_events(second_run)] == list(range(1, 10))

    assert len(aer.get_errors(first_run)) == 1
    assert len(aer.get_errors(second_run)) == 1
    assert len(aer.get_recoveries(first_run)) == 1
    assert len(aer.get_recoveries(second_run)) == 1


def test_an_unrecovered_failure_still_leaves_a_complete_trace(aer: AER) -> None:
    """The mirror case: the agent gives up. The trace must still be whole."""
    context = aer.start_run(task="unrecoverable task", task_type="wordpress")

    attempt = context.tool(TOOL, input=PAYLOAD)
    with pytest.raises(ConnectionError, match="DNS"), attempt:
        raise ConnectionError("DNS lookup failed")

    context.fail(final_score=0.0)

    events = aer.get_events(context.run_id)
    assert [event.event_type for event in events] == [
        EventType.TASK_START,
        EventType.TOOL_CALL,
        EventType.ERROR,
        EventType.TOOL_RESULT,
        EventType.TASK_END,
    ]

    error = aer.get_errors(context.run_id)[0]
    assert error.resolved is False

    run = aer.get_run(context.run_id)
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert aer.get_recoveries(context.run_id) == []
