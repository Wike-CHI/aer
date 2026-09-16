"""Restart-persistence tests (Milestone 2, Task 2.5).

The mandatory scenario for this milestone:

    create a run -> create 3 events -> close the database -> reopen
    -> read the run -> read the 3 events -> the order is still correct
"""

from __future__ import annotations

from pathlib import Path

from aer import AER, Event, EventType, RunStatus


def test_run_and_events_survive_a_full_restart(data_dir: Path) -> None:
    db_path = data_dir / "aer.db"

    first = AER(data_dir)
    context = first.start_run(task="Restart check", task_type="wordpress")
    for index in range(3):
        context.emit(EventType.MODEL_CALL, input={"step": index})
    first.close()

    assert first.is_closed is True
    assert db_path.is_file()

    second = AER(data_dir)
    try:
        run = second.get_run(context.run_id)
        assert run is not None
        assert run.task_description == "Restart check"
        assert run.task_type == "wordpress"
        assert run.status is RunStatus.RUNNING

        events = second.get_events(context.run_id)
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.MODEL_CALL,
            EventType.MODEL_CALL,
            EventType.MODEL_CALL,
        ]
        assert [event.input for event in events[1:]] == [
            {"step": 0},
            {"step": 1},
            {"step": 2},
        ]
    finally:
        second.close()


def test_sequence_continues_after_a_restart(data_dir: Path) -> None:
    """A run resumed in a new process must not restart numbering at 1."""
    first = AER(data_dir)
    context = first.start_run(task="Resume check")
    context.emit(EventType.MODEL_CALL)
    run_id = context.run_id
    first.close()

    second = AER(data_dir)
    try:
        assert second.events.get_next_sequence(run_id) == 3

        resumed = second.events.create(Event(run_id=run_id, event_type=EventType.MODEL_CALL))
        assert resumed.sequence == 3
    finally:
        second.close()


def test_finished_run_survives_a_restart(data_dir: Path) -> None:
    first = AER(data_dir)
    context = first.start_run(task="Finish check", task_type="seo")
    context.emit(EventType.TOOL_RESULT, output={"status": 200}, duration_ms=15)
    context.success(final_score=0.95, metadata={"verified_by": "human"})
    run_id = context.run_id
    first.close()

    second = AER(data_dir)
    try:
        run = second.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.SUCCESS
        assert run.ended_at is not None
        assert run.final_score == 0.95
        assert run.metadata == {"verified_by": "human"}

        events = second.get_events(run_id)
        assert [event.sequence for event in events] == [1, 2, 3]
        assert events[-1].event_type is EventType.TASK_END

        # The stored `TASK_END` payload must still be readable JSON.
        assert events[-1].output is not None
        assert events[-1].output["status"] == "SUCCESS"

        # And the tool result payload round-tripped as well.
        assert events[1].output == {"status": 200}
        assert events[1].duration_ms == 15
    finally:
        second.close()


def test_multiple_runs_keep_their_own_event_order(data_dir: Path) -> None:
    first = AER(data_dir)
    alpha = first.start_run(task="alpha")
    beta = first.start_run(task="beta")
    alpha.emit(EventType.MODEL_CALL)
    alpha.emit(EventType.MODEL_CALL)
    beta.emit(EventType.ERROR)
    alpha_id, beta_id = alpha.run_id, beta.run_id
    first.close()

    second = AER(data_dir)
    try:
        assert [event.sequence for event in second.get_events(alpha_id)] == [1, 2, 3]
        assert [event.sequence for event in second.get_events(beta_id)] == [1, 2]

        stored_ids = {run.id for run in second.list_runs()}
        assert stored_ids == {alpha_id, beta_id}
    finally:
        second.close()
