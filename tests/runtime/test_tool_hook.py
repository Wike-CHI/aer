"""Tool hook tests (Milestone 3, section 28).

Covers the full contract of ``run.tool(...)``: the happy path, the recorded
payloads, duration measurement, the no-result case, and the failure path where
the closing event must still be written before the exception propagates.
"""

from __future__ import annotations

import time

import pytest

from aer import AER, EventType, HookStateError, RunStateError, StorageError
from aer.runtime.serialization import FALLBACK_MARKER_KEY

TOOL = "wordpress.update_page"


class TestToolSuccess:
    def test_records_call_then_result(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL, input={"page_id": 123}) as tool:
            tool.set_result({"status": 200})

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3]

        call, result = events[1], events[2]
        assert call.input == {"tool": TOOL, "arguments": {"page_id": 123}}
        assert call.metadata == {"hook": "tool", "tool": TOOL}
        assert call.duration_ms is None

        assert result.output == {
            "tool": TOOL,
            "success": True,
            "result": {"status": 200},
        }
        assert result.duration_ms is not None

    def test_captures_nothing_from_the_block_scope(self, aer: AER) -> None:
        """AER never guesses a return value (section 6)."""
        context = aer.start_run(task="task")

        with context.tool(TOOL) as tool:
            computed = 6 * 7  # noqa: F841 - deliberately unused

        result = aer.get_events(context.run_id)[-1]
        assert result.output == {"tool": TOOL, "success": True, "result": None}
        assert tool.result is None

    def test_set_result_twice_keeps_the_last_value(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL) as tool:
            tool.set_result("first")
            tool.set_result("second")

        assert aer.get_events(context.run_id)[-1].output["result"] == "second"

    def test_non_json_result_becomes_a_marker(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL) as tool:
            tool.set_result(object())

        result = aer.get_events(context.run_id)[-1].output
        assert result is not None
        assert result["result"][FALLBACK_MARKER_KEY] is True

    def test_exposes_its_events_and_state(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL) as tool:
            assert tool.call_event is not None
            assert tool.call_event.sequence == 2
            assert tool.is_closed is False
            tool.set_result(1)

        assert tool.name == TOOL
        assert tool.hook_name == "tool"
        assert tool.run is context
        assert tool.result_event is not None
        assert tool.result_event.sequence == 3
        assert tool.is_closed is True
        assert tool.result == 1

    def test_result_is_persisted_not_just_returned(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        with context.tool(TOOL) as tool:
            tool.set_result({"nested": {"deep": [1, 2]}})

        assert aer.get_events(context.run_id)[-1].output["result"] == {"nested": {"deep": [1, 2]}}


class TestToolDuration:
    def test_duration_comes_from_the_monotonic_clock(
        self, aer: AER, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wall-clock arithmetic is forbidden for durations (section 7)."""
        context = aer.start_run(task="task")

        readings: list[float] = []

        def fake_perf_counter() -> float:
            readings.append(float(len(readings)))
            # First reading = start, second = stop: exactly 250 ms apart.
            return 100.0 if len(readings) == 1 else 100.25

        monkeypatch.setattr("aer.runtime.hooks.perf_counter", fake_perf_counter)

        with context.tool(TOOL) as tool:
            pass

        assert len(readings) == 2, "expected exactly one start and one stop reading"
        assert tool.result_event is not None
        assert tool.result_event.duration_ms == 250

    def test_duration_reflects_real_elapsed_time(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL) as tool:
            time.sleep(0.05)

        assert tool.result_event is not None
        assert tool.result_event.duration_ms is not None
        assert tool.result_event.duration_ms >= 30


class TestToolFailure:
    def test_records_call_error_then_result(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        attempt = context.tool(TOOL, input={"page_id": 123})
        with pytest.raises(RuntimeError, match="403 Forbidden"), attempt:
            raise RuntimeError("403 Forbidden")

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TOOL_CALL,
            EventType.ERROR,
            EventType.TOOL_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4]

        error_event = events[2]
        assert error_event.input == {
            "error_type": "builtins.RuntimeError",
            "error_message": "403 Forbidden",
            "recoverable": True,
        }
        assert error_event.metadata == {"hook": "tool", "tool": TOOL}

        result = events[3].output
        assert result is not None
        assert result["tool"] == TOOL
        assert result["success"] is False
        assert result["error"]["error_type"] == "builtins.RuntimeError"
        assert result["error"]["error_message"] == "403 Forbidden"
        assert result["error"]["error_id"]

    def test_the_failure_is_persisted_as_a_structured_error(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError), context.tool(TOOL):
            raise RuntimeError("403 Forbidden")

        errors = aer.get_errors(context.run_id)
        assert len(errors) == 1

        record = errors[0]
        assert record.error_type == "builtins.RuntimeError"
        assert record.error_message == "403 Forbidden"
        assert record.resolved is False
        assert record.event_id is not None
        assert record.metadata == {"hook": "tool", "tool": TOOL}

    def test_the_exception_is_not_swallowed(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        sentinel = ValueError("boom")

        with pytest.raises(ValueError) as excinfo, context.tool(TOOL):
            raise sentinel

        assert excinfo.value is sentinel

    def test_a_tool_failure_does_not_fail_the_run(self, aer: AER) -> None:
        """A failing tool call leaves the run RUNNING -- the agent may recover."""
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError), context.tool(TOOL):
            raise RuntimeError("403 Forbidden")

        assert context.is_finished is False
        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.status.value == "RUNNING"

    def test_the_run_can_still_succeed_after_a_tool_failure(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError), context.tool(TOOL):
            raise RuntimeError("403 Forbidden")

        run = context.success()
        assert run.status.value == "SUCCESS"

    def test_a_recording_failure_does_not_replace_the_original_exception(
        self, aer: AER, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller's exception must win even when AER cannot record it."""
        from aer.storage.repositories import ErrorRepository

        def exploding_create(self: ErrorRepository, error: object) -> object:
            raise StorageError("storage is unavailable")

        monkeypatch.setattr(ErrorRepository, "create", exploding_create)

        context = aer.start_run(task="task")
        sentinel = RuntimeError("403 Forbidden")

        with pytest.raises(RuntimeError) as excinfo, context.tool(TOOL):
            raise sentinel

        assert excinfo.value is sentinel
        notes = getattr(excinfo.value, "__notes__", [])
        assert any("could not record" in note for note in notes), notes


class TestToolGuards:
    def test_cannot_open_a_tool_hook_on_a_finished_run(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError, match="already finished"), context.tool(TOOL):
            pass  # pragma: no cover - __enter__ raises

    def test_a_context_object_cannot_be_reused(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        tool = context.tool(TOOL)

        with tool:
            pass

        with pytest.raises(HookStateError, match="already been used"), tool:
            pass  # pragma: no cover - __enter__ raises

    def test_set_result_outside_the_block_is_rejected(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        tool = context.tool(TOOL)

        with pytest.raises(HookStateError, match="not open"):
            tool.set_result(1)

    def test_tool_names_are_recorded_verbatim(self, aer: AER) -> None:
        """Hooks hold no domain knowledge; they never interpret the name (section 35)."""
        context = aer.start_run(task="task")
        with context.tool("anything.at.all"):
            pass

        assert aer.get_events(context.run_id)[1].input["tool"] == "anything.at.all"


class TestToolMetadata:
    def test_caller_metadata_is_merged_and_attached_to_both_events(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL, metadata={"attempt": 2}):
            pass

        call, result = aer.get_events(context.run_id)[1:]
        assert call.metadata == {"attempt": 2, "hook": "tool", "tool": TOOL}
        assert result.metadata == {"attempt": 2, "hook": "tool", "tool": TOOL}

    def test_caller_metadata_cannot_spoof_the_hook_marker(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.tool(TOOL, metadata={"hook": "manual", "tool": "evil"}):
            pass

        metadata = aer.get_events(context.run_id)[1].metadata
        assert metadata["hook"] == "tool"
        assert metadata["tool"] == TOOL
