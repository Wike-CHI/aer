"""Event capture tests (Milestone 1, Task 3.3 + the ``emit`` SDK surface)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aer import AER, EventType
from aer.runtime.serialization import FALLBACK_MARKER_KEY


class TestEmit:
    def test_returns_a_persisted_event(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        event = context.emit(
            EventType.MODEL_CALL,
            input={"prompt": "test"},
            metadata={"model": "gpt-4.1"},
        )

        assert event.id is not None
        assert event.sequence == 2
        assert event.run_id == context.run_id
        assert event.event_type is EventType.MODEL_CALL
        assert event.input == {"prompt": "test"}
        assert event.output is None
        assert event.duration_ms is None
        assert event.metadata == {"model": "gpt-4.1"}
        assert event.is_persisted is True

    def test_records_duration_and_output(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        event = context.emit(
            EventType.TOOL_RESULT,
            output={"status": 200, "body": "ok"},
            duration_ms=42,
        )

        assert event.duration_ms == 42
        assert event.output == {"status": 200, "body": "ok"}

    def test_unknown_payloads_are_marked_not_flattened(self, aer: AER) -> None:
        """A degraded payload must stay identifiable in the stored trace."""
        context = aer.start_run(task="task")

        class Response:
            def __repr__(self) -> str:
                return "<Response 200>"

        event = context.emit(EventType.TOOL_RESULT, output={"response": Response()})

        assert event.output is not None
        marker = event.output["response"]
        assert marker[FALLBACK_MARKER_KEY] is True
        assert "<Response 200>" in marker["repr"]

        # and it round-trips through SQLite unchanged
        stored = aer.get_events(context.run_id)[-1]
        assert stored.output == event.output

    def test_rejects_a_duration_that_is_not_a_number(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(ValidationError):
            context.emit(EventType.TOOL_CALL, duration_ms="soon")  # type: ignore[arg-type]


class TestSequence:
    def test_sequence_starts_at_one_and_increases_by_one(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        for index in range(4):
            context.emit(EventType.MODEL_CALL, input={"index": index})

        events = aer.get_events(context.run_id)
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
        assert [event.input for event in events[1:]] == [
            {"index": 0},
            {"index": 1},
            {"index": 2},
            {"index": 3},
        ]

    def test_sequence_keeps_increasing_after_completion(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.emit(EventType.MODEL_CALL)
        context.success()

        events = aer.get_events(context.run_id)
        assert [event.sequence for event in events] == [1, 2, 3]
        assert events[-1].event_type is EventType.TASK_END

    def test_each_run_numbers_its_own_events(self, aer: AER) -> None:
        first = aer.start_run(task="first")
        second = aer.start_run(task="second")

        first.emit(EventType.MODEL_CALL)
        first.emit(EventType.MODEL_CALL)
        second.emit(EventType.TOOL_CALL)

        assert [event.sequence for event in aer.get_events(first.run_id)] == [1, 2, 3]
        assert [event.sequence for event in aer.get_events(second.run_id)] == [1, 2]

    def test_get_next_sequence_is_derived_from_storage(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        assert aer.events.get_next_sequence(context.run_id) == 2

        context.emit(EventType.ERROR)
        assert aer.events.get_next_sequence(context.run_id) == 3

    def test_get_next_sequence_of_an_unknown_run_is_one(self, aer: AER) -> None:
        assert aer.events.get_next_sequence("does-not-exist") == 1


class TestEventReads:
    def test_events_are_returned_ordered_by_sequence(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        for index in range(3):
            context.emit(EventType.TOOL_CALL, input={"index": index})

        events = aer.get_events(context.run_id)
        assert [event.input for event in events[1:]] == [
            {"index": 0},
            {"index": 1},
            {"index": 2},
        ]

    def test_events_can_be_paginated(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        for _ in range(3):
            context.emit(EventType.MODEL_CALL)

        page = aer.events.get_by_run(context.run_id, limit=2, offset=1)
        assert [event.sequence for event in page] == [2, 3]

    def test_events_of_an_unknown_run_are_empty(self, aer: AER) -> None:
        assert aer.get_events("does-not-exist") == []

    def test_count_by_run(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.emit(EventType.ERROR)

        assert aer.events.count_by_run(context.run_id) == 2
