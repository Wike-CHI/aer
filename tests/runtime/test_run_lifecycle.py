"""Run lifecycle tests (Milestone 1, Task 1.2 + Task 3.7).

Covers: run creation, the ``TASK_START`` hook, every completion path, and the
state-machine guard rails.
"""

from __future__ import annotations

import pytest

from aer import AER, EventType, RunStateError, RunStatus


class TestStartRun:
    def test_creates_and_persists_the_run(self, aer: AER) -> None:
        context = aer.start_run(task="Fix WordPress H1", task_type="wordpress")

        assert context.status is RunStatus.RUNNING
        assert context.is_finished is False

        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.task_description == "Fix WordPress H1"
        assert stored.task_type == "wordpress"
        assert stored.status is RunStatus.RUNNING
        assert stored.ended_at is None

    def test_accepts_the_optional_agent_identity(self, aer: AER) -> None:
        context = aer.start_run(
            task="task",
            task_type="wordpress",
            agent_name="wp-agent",
            agent_version="1.2.0",
            model_provider="openai",
            model_name="gpt-4.1",
            metadata={"tenant": "demo"},
        )

        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.agent_name == "wp-agent"
        assert stored.agent_version == "1.2.0"
        assert stored.model_provider == "openai"
        assert stored.model_name == "gpt-4.1"
        assert stored.metadata == {"tenant": "demo"}

    def test_records_task_start_automatically(self, aer: AER) -> None:
        context = aer.start_run(task="Fix WordPress H1", task_type="wordpress")

        events = aer.get_events(context.run_id)
        assert len(events) == 1

        start = events[0]
        assert start.event_type is EventType.TASK_START
        assert start.sequence == 1
        assert start.run_id == context.run_id
        assert start.input == {"task": "Fix WordPress H1", "task_type": "wordpress"}

    def test_each_run_gets_its_own_identifier(self, aer: AER) -> None:
        first = aer.start_run(task="a")
        second = aer.start_run(task="b")

        assert first.run_id != second.run_id
        assert len(aer.list_runs()) == 2

    def test_explicit_run_id_is_honoured(self, aer: AER) -> None:
        context = aer.start_run(task="task", run_id="run_20260915_001")

        assert context.run_id == "run_20260915_001"
        assert aer.get_run("run_20260915_001") is not None


class TestCompletion:
    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            ("success", RunStatus.SUCCESS),
            ("partial_success", RunStatus.PARTIAL_SUCCESS),
            ("fail", RunStatus.FAILED),
            ("abort", RunStatus.ABORTED),
        ],
    )
    def test_terminal_methods_update_status_and_end_time(
        self, aer: AER, method: str, expected: RunStatus
    ) -> None:
        context = aer.start_run(task="task")
        run = getattr(context, method)()

        assert run.status is expected
        assert run.ended_at is not None
        assert context.is_finished is True

        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.status is expected
        assert stored.ended_at is not None

    @pytest.mark.parametrize(
        "method",
        ["success", "partial_success", "fail", "abort"],
    )
    def test_terminal_methods_record_task_end(self, aer: AER, method: str) -> None:
        context = aer.start_run(task="task")
        getattr(context, method)()

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TASK_END,
        ]

        end = events[-1]
        assert end.sequence == 2
        assert end.output is not None
        assert end.output["status"] == context.status.value
        assert end.duration_ms is not None
        assert end.duration_ms >= 0

    def test_success_records_the_final_score_and_extra_metadata(self, aer: AER) -> None:
        context = aer.start_run(task="task", metadata={"tenant": "demo"})
        run = context.success(final_score=0.87, metadata={"tokens": 1234})

        assert run.final_score == 0.87
        assert run.metadata == {"tenant": "demo", "tokens": 1234}

        stored = aer.get_run(context.run_id)
        assert stored is not None
        assert stored.final_score == 0.87
        assert stored.metadata == {"tenant": "demo", "tokens": 1234}

    def test_finishing_twice_raises(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError, match="already finished"):
            context.fail()

    def test_finish_rejects_non_terminal_status(self, aer: AER) -> None:
        context = aer.start_run(task="task")

        with pytest.raises(RunStateError, match="not a terminal run status"):
            context.finish(RunStatus.RUNNING)

    def test_emit_after_completion_raises(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError, match="already finished"):
            context.emit(EventType.MODEL_CALL, input={"prompt": "late"})

    def test_success_declares_completion_but_does_not_verify_it(self, aer: AER) -> None:
        """``success()`` is an agent claim, not a verification verdict.

        The distinction must stay representable: nothing in this milestone writes a
        verification record, and the run exposes no ``verified`` flag.
        """
        context = aer.start_run(task="task")
        run = context.success()

        assert run.status is RunStatus.SUCCESS
        assert not hasattr(run, "verified")
        assert not hasattr(run, "verified_status")
