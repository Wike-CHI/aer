"""Error pipeline tests (Milestone 3, sections 12, 13, 14).

``run.error(...)`` is the single error pipeline in AER: the hooks call it too, so
these tests pin the shape that every recorded failure shares.
"""

from __future__ import annotations

import pytest

from aer import AER, EventType, RunStateError
from aer.runtime.sanitization import REDACTED


def raise_permission_error(message: str = "403 Forbidden") -> None:
    """Raise from its own frame so the recorded stack trace has something to show."""
    raise PermissionError(message)


def capture_error(aer: AER, message: str = "403 Forbidden"):
    """Run a failing call and record it, returning ``(context, record)``."""
    context = aer.start_run(task="task")
    with pytest.raises(PermissionError) as excinfo:
        raise_permission_error(message)
    return context, context.error(excinfo.value)


class TestErrorEvent:
    def test_creates_an_error_event(self, aer: AER) -> None:
        context, record = capture_error(aer)

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.ERROR,
        ]

        error_event = events[1]
        assert error_event.sequence == 2
        assert error_event.input == {
            "error_type": "builtins.PermissionError",
            "error_message": "403 Forbidden",
            "recoverable": True,
        }
        assert record.event_id == error_event.id

    def test_the_event_is_written_before_the_record(self, aer: AER) -> None:
        """An ErrorRecord always has its event (section 12)."""
        context, record = capture_error(aer)

        assert record.event_id is not None
        looked_up = aer.events.get(record.event_id)
        assert looked_up is not None
        assert looked_up.run_id == context.run_id


class TestErrorRecord:
    def test_records_type_message_and_stack_trace(self, aer: AER) -> None:
        _, record = capture_error(aer)

        assert record.error_type == "builtins.PermissionError"
        assert record.error_message == "403 Forbidden"
        assert record.stack_trace is not None
        assert "Traceback (most recent call last)" in record.stack_trace
        assert "raise_permission_error" in record.stack_trace
        assert "PermissionError: 403 Forbidden" in record.stack_trace

    def test_defaults_are_recoverable_and_unresolved(self, aer: AER) -> None:
        _, record = capture_error(aer)

        assert record.recoverable is True
        assert record.resolved is False

    def test_recoverability_can_be_denied(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(PermissionError) as excinfo:
            raise_permission_error()

        record = context.error(excinfo.value, recoverable=False)

        assert record.recoverable is False
        stored = aer.get_error(record.id)
        assert stored is not None
        assert stored.recoverable is False

    def test_is_persisted_and_rereadable(self, aer: AER) -> None:
        context, record = capture_error(aer)

        stored = aer.get_error(record.id)
        assert stored is not None
        assert stored.run_id == context.run_id
        assert stored == record
        assert aer.get_error("no-such-error") is None
        assert aer.get_errors(context.run_id) == [record]

    def test_metadata_is_attached(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(PermissionError) as excinfo:
            raise_permission_error()

        record = context.error(excinfo.value, metadata={"attempt": 1})

        assert record.metadata == {"attempt": 1}
        stored_event = aer.events.get(record.event_id)  # type: ignore[arg-type]
        assert stored_event is not None
        assert stored_event.metadata == {"attempt": 1}

    def test_an_empty_message_still_records_something_usable(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError) as excinfo:
            raise RuntimeError()

        record = context.error(excinfo.value)

        assert record.error_message != ""

    def test_multiple_errors_are_distinct_records(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        first = context.error(RuntimeError("one"))
        second = context.error(RuntimeError("two"))

        assert first.id != second.id
        assert [error.error_message for error in aer.get_errors(context.run_id)] == [
            "one",
            "two",
        ]


class TestErrorDoesNotFailTheRun:
    def test_recording_an_error_leaves_the_run_running(self, aer: AER) -> None:
        context, _ = capture_error(aer)

        assert context.is_finished is False
        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.status.value == "RUNNING"

    def test_a_run_with_errors_can_still_succeed(self, aer: AER) -> None:
        """Recovery exists precisely so this sequence is legal (section 27)."""
        context, _ = capture_error(aer)

        run = context.success()

        assert run.status.value == "SUCCESS"
        assert len(aer.get_errors(context.run_id)) == 1


class TestErrorSanitisation:
    def test_stack_trace_is_redacted(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError) as excinfo:
            raise RuntimeError("request failed with Authorization: Bearer sk-live-x9f2a7c1")

        record = context.error(excinfo.value)

        assert record.stack_trace is not None
        assert "sk-live-x9f2a7c1" not in record.stack_trace
        assert REDACTED in record.stack_trace

    def test_the_message_is_redacted_too(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError) as excinfo:
            raise RuntimeError("api_key=SUPERSECRETVALUE rejected")

        record = context.error(excinfo.value)

        assert "SUPERSECRETVALUE" not in record.error_message
        assert REDACTED in record.error_message

    def test_a_plain_message_is_left_alone(self, aer: AER) -> None:
        _, record = capture_error(aer)

        assert record.error_message == "403 Forbidden"


class TestErrorGuards:
    def test_error_after_completion_is_rejected(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError, match="already finished"):
            context.error(RuntimeError("too late"))

    def test_a_rejected_error_writes_nothing(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError):
            context.error(RuntimeError("too late"))

        assert aer.get_errors(context.run_id) == []
        assert [event.event_type for event in aer.get_events(context.run_id)] == [
            EventType.TASK_START,
            EventType.TASK_END,
        ]
