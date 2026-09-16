"""Repository tests (Milestone 2, Task 2.4).

These exercise the repository layer directly, without the ``RunContext`` sugar, so
that the contract of each repository method is pinned down on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aer import AER, Event, EventType, Run, RunStatus, StorageError
from aer.exceptions import RecordNotFoundError

BASE_TIME = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def make_run(
    run_id: str,
    *,
    started_at: datetime = BASE_TIME,
    status: RunStatus = RunStatus.RUNNING,
    task_type: str | None = "wordpress",
) -> Run:
    return Run(
        id=run_id,
        task_description=f"task {run_id}",
        task_type=task_type,
        status=status,
        started_at=started_at,
    )


class TestRunRepository:
    def test_create_then_get_round_trips_every_field(self, aer: AER) -> None:
        run = Run(
            id="run-1",
            task_description="Fix WordPress H1",
            task_type="wordpress",
            agent_name="wp-agent",
            agent_version="1.2.0",
            model_provider="openai",
            model_name="gpt-4.1",
            status=RunStatus.SUCCESS,
            started_at=BASE_TIME,
            ended_at=BASE_TIME + timedelta(seconds=3),
            final_score=0.75,
            metadata={"tenant": "demo", "retries": 2},
        )

        aer.runs.create(run)
        loaded = aer.runs.get("run-1")

        assert loaded is not None
        assert loaded == run
        assert loaded.started_at.tzinfo is not None
        assert loaded.ended_at is not None and loaded.ended_at.tzinfo is not None

    def test_get_returns_none_for_an_unknown_id(self, aer: AER) -> None:
        assert aer.runs.get("nope") is None

    def test_create_twice_with_the_same_id_fails(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))

        with pytest.raises(StorageError):
            aer.runs.create(make_run("run-1"))

    def test_update_persists_the_new_state(self, aer: AER) -> None:
        run = aer.runs.create(make_run("run-1"))

        run.status = RunStatus.FAILED
        run.ended_at = BASE_TIME + timedelta(seconds=5)
        run.final_score = 0.1
        run.metadata["reason"] = "timeout"
        aer.runs.update(run)

        loaded = aer.runs.get("run-1")
        assert loaded is not None
        assert loaded.status is RunStatus.FAILED
        assert loaded.ended_at == BASE_TIME + timedelta(seconds=5)
        assert loaded.final_score == 0.1
        assert loaded.metadata == {"reason": "timeout"}

    def test_update_of_an_unknown_run_raises(self, aer: AER) -> None:
        with pytest.raises(RecordNotFoundError, match="Run not found"):
            aer.runs.update(make_run("ghost"))

    def test_list_returns_newest_first_by_default(self, aer: AER) -> None:
        aer.runs.create(make_run("older", started_at=BASE_TIME))
        aer.runs.create(make_run("newer", started_at=BASE_TIME + timedelta(hours=1)))

        assert [run.id for run in aer.runs.list()] == ["newer", "older"]
        assert [run.id for run in aer.runs.list(newest_first=False)] == ["older", "newer"]

    def test_list_supports_pagination(self, aer: AER) -> None:
        for index in range(3):
            aer.runs.create(
                make_run(f"run-{index}", started_at=BASE_TIME + timedelta(minutes=index))
            )

        page = aer.runs.list(limit=1, offset=1, newest_first=False)
        assert [run.id for run in page] == ["run-1"]

    def test_list_filters_by_status_and_task_type(self, aer: AER) -> None:
        aer.runs.create(make_run("ok", status=RunStatus.SUCCESS))
        aer.runs.create(make_run("bad", status=RunStatus.FAILED))
        aer.runs.create(make_run("other", status=RunStatus.SUCCESS, task_type="seo"))

        assert {run.id for run in aer.runs.list(status=RunStatus.SUCCESS)} == {"ok", "other"}
        assert {run.id for run in aer.runs.list(task_type="wordpress")} == {"ok", "bad"}
        assert [
            run.id for run in aer.runs.list(status=RunStatus.SUCCESS, task_type="wordpress")
        ] == ["ok"]

    def test_count_matches_the_stored_rows(self, aer: AER) -> None:
        aer.runs.create(make_run("a", status=RunStatus.SUCCESS))
        aer.runs.create(make_run("b", status=RunStatus.FAILED))

        assert aer.runs.count() == 2
        assert aer.runs.count(status=RunStatus.FAILED) == 1

    def test_unknown_stored_status_is_reported_clearly(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))

        with aer.database.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE runs SET status = 'BOGUS' WHERE id = 'run-1'")

        with pytest.raises(StorageError, match="unknown value"):
            aer.runs.get("run-1")


class TestEventRepository:
    def test_create_allocates_the_sequence(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))

        first = aer.events.create(Event(run_id="run-1", event_type=EventType.TASK_START))
        second = aer.events.create(Event(run_id="run-1", event_type=EventType.MODEL_CALL))

        assert (first.sequence, second.sequence) == (1, 2)
        assert first.id is not None and second.id is not None
        assert second.id > first.id

    def test_create_honours_an_explicit_sequence(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))

        event = aer.events.create(Event(run_id="run-1", event_type=EventType.ERROR, sequence=7))

        assert event.sequence == 7
        assert aer.events.get_next_sequence("run-1") == 8

    def test_duplicate_sequence_is_rejected(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))
        aer.events.create(Event(run_id="run-1", event_type=EventType.ERROR, sequence=1))

        with pytest.raises(StorageError):
            aer.events.create(Event(run_id="run-1", event_type=EventType.ERROR, sequence=1))

    def test_foreign_key_to_a_missing_run_is_rejected(self, aer: AER) -> None:
        with pytest.raises(StorageError):
            aer.events.create(Event(run_id="missing", event_type=EventType.TASK_START))

    def test_get_by_run_orders_by_sequence_not_by_timestamp(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))

        # Deliberately write "later" events with earlier timestamps.
        for index in range(3):
            aer.events.create(
                Event(
                    run_id="run-1",
                    event_type=EventType.MODEL_CALL,
                    input={"index": index},
                    created_at=BASE_TIME - timedelta(minutes=index),
                )
            )

        events = aer.events.get_by_run("run-1")
        assert [event.sequence for event in events] == [1, 2, 3]
        assert [event.input for event in events] == [
            {"index": 0},
            {"index": 1},
            {"index": 2},
        ]

    def test_get_by_run_scopes_events_to_one_run(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))
        aer.runs.create(make_run("run-2"))
        aer.events.create(Event(run_id="run-1", event_type=EventType.TASK_START))
        aer.events.create(Event(run_id="run-2", event_type=EventType.TASK_START))

        events = aer.events.get_by_run("run-1")
        assert len(events) == 1
        assert events[0].run_id == "run-1"

    def test_get_by_run_of_an_unknown_run_is_empty(self, aer: AER) -> None:
        assert aer.events.get_by_run("missing") == []

    def test_get_loads_a_single_event(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))
        created = aer.events.create(
            Event(
                run_id="run-1",
                event_type=EventType.TOOL_RESULT,
                output={"status": 200},
                duration_ms=12,
                metadata={"tool": "wordpress.update_page"},
            )
        )

        assert created.id is not None
        loaded = aer.events.get(created.id)

        assert loaded is not None
        assert loaded == created

    def test_get_returns_none_for_an_unknown_id(self, aer: AER) -> None:
        assert aer.events.get(9999) is None

    def test_count_by_run(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))
        aer.events.create(Event(run_id="run-1", event_type=EventType.TASK_START))
        aer.events.create(Event(run_id="run-1", event_type=EventType.TASK_END))

        assert aer.events.count_by_run("run-1") == 2
        assert aer.events.count_by_run("run-2") == 0

    def test_get_next_sequence_of_an_empty_run_is_one(self, aer: AER) -> None:
        aer.runs.create(make_run("run-1"))
        assert aer.events.get_next_sequence("run-1") == 1
