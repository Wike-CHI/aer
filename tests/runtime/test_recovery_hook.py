"""Recovery hook tests (Milestone 3, sections 15-20, 30).

Covers the happy path, the failure path, explicit error linking, the resolution
rule (a successful linked recovery resolves exactly one error, and nothing else
ever does), and the interaction with a surrounding tool hook.
"""

from __future__ import annotations

import pytest

from aer import AER, AERError, EventType, HookStateError, RecordNotFoundError, RunStateError


def failing_error(aer: AER, message: str = "403 Forbidden"):
    """Record a failure and return ``(context, error)``."""
    context = aer.start_run(task="task")
    return context, context.error(PermissionError(message))


class TestRecoverySuccess:
    def test_records_start_and_result(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.recovery(reason="REST API returned 403") as recovery:
            recovery.set_result({"fixed": True})

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.RECOVERY_START,
            EventType.RECOVERY_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3]

        assert events[1].input == {"reason": "REST API returned 403", "error_id": None}
        assert events[1].metadata == {"hook": "recovery"}

        assert events[2].output == {
            "reason": "REST API returned 403",
            "success": True,
            "error_id": None,
            "result": {"fixed": True},
        }
        assert events[2].duration_ms is not None

    def test_persists_a_recovery_record(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.recovery(reason="403") as recovery:
            recovery.set_result("fixed")

        record = recovery.record
        assert record is not None
        assert record.run_id == context.run_id
        assert record.reason == "403"
        assert record.success is True
        assert record.duration_ms is not None
        assert record.duration_ms >= 0
        assert record.started_at.tzinfo is not None
        assert record.ended_at is not None
        assert record.outcome == "fixed"
        assert record.start_event_id is not None
        assert record.result_event_id is not None
        assert record.result_event_id > record.start_event_id

        stored = aer.recoveries.get(record.id)
        assert stored == record
        assert aer.get_recoveries(context.run_id) == [record]

    def test_outcome_is_optional(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with context.recovery(reason="restart the service"):
            pass

        result = aer.get_events(context.run_id)[-1].output
        assert result is not None
        assert result["success"] is True
        assert result["result"] is None

        records = aer.get_recoveries(context.run_id)
        assert records[0].outcome is None

    def test_exposes_state(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        recovery = context.recovery(reason="403")

        assert recovery.hook_name == "recovery"
        assert recovery.reason == "403"
        assert recovery.opening_event is None
        assert recovery.succeeded is None

        with recovery:
            assert recovery.opening_event is not None
            assert recovery.succeeded is None

        assert recovery.is_closed is True
        assert recovery.succeeded is True
        assert recovery.result_event is not None


class TestRecoveryFailure:
    def test_records_start_error_then_result(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        recovery = context.recovery(reason="REST API 403")
        with pytest.raises(RuntimeError, match="still forbidden"), recovery:
            raise RuntimeError("still forbidden")

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.RECOVERY_START,
            EventType.ERROR,
            EventType.RECOVERY_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4]

        result = events[3].output
        assert result is not None
        assert result["success"] is False
        assert result["error"]["error_type"] == "builtins.RuntimeError"

        record = recovery.record
        assert record is not None
        assert record.success is False
        assert record.result_event_id == events[3].id
        assert len(aer.get_errors(context.run_id)) == 1

    def test_the_exception_still_propagates(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        sentinel = RuntimeError("still forbidden")

        with pytest.raises(RuntimeError) as excinfo, context.recovery(reason="403"):
            raise sentinel

        assert excinfo.value is sentinel

    def test_a_failed_recovery_does_not_resolve_its_error(self, aer: AER) -> None:
        context, error = failing_error(aer)

        with pytest.raises(RuntimeError), context.recovery(reason="403", error_id=error.id):
            raise RuntimeError("still forbidden")

        stored = aer.get_error(error.id)
        assert stored is not None
        assert stored.resolved is False

        records = aer.get_recoveries(context.run_id)
        assert records[0].error_id == error.id
        assert records[0].success is False

    def test_duration_comes_from_the_monotonic_clock(
        self, aer: AER, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = aer.start_run(task="task")

        readings: list[float] = []

        def fake_perf_counter() -> float:
            readings.append(float(len(readings)))
            return 10.0 if len(readings) == 1 else 10.5

        monkeypatch.setattr("aer.runtime.hooks.perf_counter", fake_perf_counter)

        with context.recovery(reason="403") as recovery:
            pass

        assert len(readings) == 2
        assert recovery.record is not None
        assert recovery.record.duration_ms == 500
        assert recovery.result_event is not None
        assert recovery.result_event.duration_ms == 500


class TestRecoveryErrorLinkage:
    def test_a_successful_linked_recovery_resolves_the_error(self, aer: AER) -> None:
        context, error = failing_error(aer)
        assert error.resolved is False

        with context.recovery(reason="fixed the capability", error_id=error.id):
            pass

        stored = aer.get_error(error.id)
        assert stored is not None
        assert stored.resolved is True

    def test_resolution_touches_only_the_linked_error(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        first = context.error(PermissionError("first"))
        second = context.error(PermissionError("second"))

        with context.recovery(reason="fix the first", error_id=first.id):
            pass

        assert aer.get_error(first.id).resolved is True  # type: ignore[union-attr]
        assert aer.get_error(second.id).resolved is False  # type: ignore[union-attr]

    def test_an_unlinked_recovery_resolves_nothing(self, aer: AER) -> None:
        """Resolution is always explicit (section 18)."""
        context, error = failing_error(aer)

        with context.recovery(reason="unrelated cleanup"):
            pass

        stored = aer.get_error(error.id)
        assert stored is not None
        assert stored.resolved is False

    def test_a_later_success_does_not_resolve_history(self, aer: AER) -> None:
        context, error = failing_error(aer)

        context.success()

        stored = aer.get_error(error.id)
        assert stored is not None
        assert stored.resolved is False

    def test_an_unknown_error_id_is_rejected_before_anything_is_written(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        recovery = context.recovery(reason="403", error_id="does-not-exist")
        with pytest.raises(RecordNotFoundError, match="unknown error"), recovery:
            pass  # pragma: no cover - __enter__ raises

        assert [event.event_type for event in aer.get_events(context.run_id)] == [
            EventType.TASK_START
        ]
        assert aer.get_recoveries(context.run_id) == []

    def test_an_error_from_another_run_is_rejected(self, aer: AER) -> None:
        _, foreign_error = failing_error(aer)
        context = aer.start_run(task="another task")

        recovery = context.recovery(reason="403", error_id=foreign_error.id)
        with pytest.raises(AERError, match="belongs to run"), recovery:
            pass  # pragma: no cover - __enter__ raises

        assert aer.get_recoveries(context.run_id) == []

    def test_records_can_be_found_by_error(self, aer: AER) -> None:
        context, error = failing_error(aer)

        with context.recovery(reason="attempt one", error_id=error.id):
            pass

        found = aer.recoveries.get_by_error(error.id)
        assert len(found) == 1
        assert found[0].error_id == error.id


class TestRecoveryGuards:
    def test_cannot_open_a_recovery_hook_on_a_finished_run(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        # Rejected at the call site rather than at `__enter__`: fail where the
        # mistake was made, not three lines later.
        with pytest.raises(RunStateError, match="already finished"):
            context.recovery(reason="too late")

    def test_a_context_object_cannot_be_reused(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        recovery = context.recovery(reason="403")

        with recovery:
            pass

        with pytest.raises(HookStateError, match="already been used"), recovery:
            pass  # pragma: no cover - __enter__ raises

    def test_a_failed_recovery_does_not_finish_the_run(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RuntimeError), context.recovery(reason="403"):
            raise RuntimeError("still forbidden")

        assert context.is_finished is False


class TestNestedHooks:
    def test_tool_with_a_nested_recovery_keeps_event_order(self, aer: AER) -> None:
        """No span tree yet, but ordering must be exact (section 22)."""
        context = aer.start_run(task="task")

        with context.tool("wordpress.update_page") as tool:
            with context.recovery(reason="warm the cache") as recovery:
                recovery.set_result("warmed")
            tool.set_result({"status": 200})

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TOOL_CALL,
            EventType.RECOVERY_START,
            EventType.RECOVERY_RESULT,
            EventType.TOOL_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5]

    def test_a_failing_nested_recovery_still_closes_both_hooks(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        tool = context.tool("wordpress.update_page")
        recovery = context.recovery(reason="403")
        with pytest.raises(RuntimeError, match="permissions still wrong"), tool, recovery:
            raise RuntimeError("permissions still wrong")

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TOOL_CALL,
            EventType.RECOVERY_START,
            EventType.ERROR,  # from the recovery
            EventType.RECOVERY_RESULT,
            EventType.ERROR,  # from the tool, carrying the same exception
            EventType.TOOL_RESULT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5, 6, 7]
        assert len(aer.get_errors(context.run_id)) == 2
        assert recovery.record is not None and recovery.record.success is False
        assert tool.result_event is not None
        assert tool.result_event.output["success"] is False
