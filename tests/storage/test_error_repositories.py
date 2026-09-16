"""ErrorRepository / RecoveryRepository contracts (Milestone 3, sections 11, 20).

Exercised directly, without hooks, so each repository method's contract is pinned
on its own -- including the foreign keys and the resolution rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aer import AER, ErrorRecord, Event, EventType, RecoveryRecord, Run, StorageError
from aer.exceptions import RecordNotFoundError

BASE_TIME = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def seed_run(aer: AER, run_id: str = "run-1", *, started_at: datetime = BASE_TIME) -> Run:
    return aer.runs.create(
        Run(
            id=run_id,
            task_description=f"task {run_id}",
            task_type="wordpress",
            started_at=started_at,
        )
    )


def make_error(error_id: str = "err-1", **overrides: object) -> ErrorRecord:
    payload: dict[str, object] = {
        "id": error_id,
        "run_id": "run-1",
        "error_type": "builtins.PermissionError",
        "error_message": "403 Forbidden",
        "stack_trace": "Traceback (most recent call last): ...",
        "recoverable": True,
        "resolved": False,
        "created_at": BASE_TIME,
        "metadata": {"tool": "wordpress.update_page"},
    }
    payload.update(overrides)
    return ErrorRecord(**payload)  # type: ignore[arg-type]


def make_recovery(recovery_id: str = "rec-1", **overrides: object) -> RecoveryRecord:
    payload: dict[str, object] = {
        "id": recovery_id,
        "run_id": "run-1",
        "reason": "REST API 403",
        "started_at": BASE_TIME,
    }
    payload.update(overrides)
    return RecoveryRecord(**payload)  # type: ignore[arg-type]


class TestErrorRepository:
    def test_round_trips_every_field(self, aer: AER) -> None:
        seed_run(aer)
        event = aer.events.create(Event(run_id="run-1", event_type=EventType.ERROR))

        error = make_error(event_id=event.id, resolved=True)
        aer.errors.create(error)

        loaded = aer.errors.get("err-1")
        assert loaded is not None
        assert loaded == error
        assert loaded.created_at.tzinfo is not None
        assert loaded.metadata == {"tool": "wordpress.update_page"}

    def test_get_returns_none_for_an_unknown_id(self, aer: AER) -> None:
        assert aer.errors.get("nope") is None

    def test_create_twice_with_the_same_id_fails(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error())

        with pytest.raises(StorageError):
            aer.errors.create(make_error())

    def test_requires_an_existing_run(self, aer: AER) -> None:
        with pytest.raises(StorageError):
            aer.errors.create(make_error(run_id="missing-run"))

    def test_requires_an_existing_event_when_linked(self, aer: AER) -> None:
        seed_run(aer)

        with pytest.raises(StorageError):
            aer.errors.create(make_error(event_id=9999))

    def test_event_id_is_optional(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error(event_id=None))

        loaded = aer.errors.get("err-1")
        assert loaded is not None
        assert loaded.event_id is None

    def test_get_by_run_is_ordered_and_scoped(self, aer: AER) -> None:
        seed_run(aer, "run-1")
        seed_run(aer, "run-2")
        aer.errors.create(make_error("e2", created_at=BASE_TIME + timedelta(minutes=1)))
        aer.errors.create(make_error("e1", created_at=BASE_TIME))
        aer.errors.create(make_error("other", run_id="run-2"))

        assert [error.id for error in aer.errors.get_by_run("run-1")] == ["e1", "e2"]
        assert [error.id for error in aer.errors.get_by_run("run-2")] == ["other"]

    def test_get_by_run_filters_on_resolved(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error("open", resolved=False))
        aer.errors.create(make_error("closed", resolved=True, created_at=BASE_TIME))

        assert [e.id for e in aer.errors.get_by_run("run-1", resolved=False)] == ["open"]
        assert [e.id for e in aer.errors.get_by_run("run-1", resolved=True)] == ["closed"]
        assert len(aer.errors.get_by_run("run-1")) == 2

    def test_mark_resolved_updates_one_error(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error())

        updated = aer.errors.mark_resolved("err-1")

        assert updated.resolved is True
        stored = aer.errors.get("err-1")
        assert stored is not None
        assert stored.resolved is True

    def test_mark_resolved_can_be_reverted(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error(resolved=True))

        aer.errors.mark_resolved("err-1", resolved=False)

        stored = aer.errors.get("err-1")
        assert stored is not None
        assert stored.resolved is False

    def test_mark_resolved_of_an_unknown_error_raises(self, aer: AER) -> None:
        with pytest.raises(RecordNotFoundError, match="Error not found"):
            aer.errors.mark_resolved("ghost")

    def test_count_by_run(self, aer: AER) -> None:
        seed_run(aer)
        aer.errors.create(make_error("a", resolved=True))
        aer.errors.create(make_error("b", resolved=False, created_at=BASE_TIME))

        assert aer.errors.count_by_run("run-1") == 2
        assert aer.errors.count_by_run("run-1", resolved=False) == 1
        assert aer.errors.count_by_run("run-2") == 0

    def test_paging(self, aer: AER) -> None:
        seed_run(aer)
        for index in range(3):
            aer.errors.create(
                make_error(f"e{index}", created_at=BASE_TIME + timedelta(minutes=index))
            )

        page = aer.errors.get_by_run("run-1", limit=1, offset=1)
        assert [error.id for error in page] == ["e1"]


class TestRecoveryRepository:
    def test_round_trips_every_field(self, aer: AER) -> None:
        seed_run(aer)
        start = aer.events.create(Event(run_id="run-1", event_type=EventType.RECOVERY_START))
        result = aer.events.create(Event(run_id="run-1", event_type=EventType.RECOVERY_RESULT))
        aer.errors.create(make_error())

        recovery = make_recovery(
            error_id="err-1",
            start_event_id=start.id,
            result_event_id=result.id,
            success=True,
            duration_ms=120,
            outcome={"fixed": True},
            ended_at=BASE_TIME + timedelta(seconds=1),
        )
        aer.recoveries.create(recovery)

        loaded = aer.recoveries.get("rec-1")
        assert loaded is not None
        assert loaded == recovery
        assert loaded.outcome == {"fixed": True}

    def test_can_be_created_open_and_completed_later(self, aer: AER) -> None:
        seed_run(aer)
        recovery = make_recovery()
        assert recovery.success is None

        aer.recoveries.create(recovery)
        loaded = aer.recoveries.get("rec-1")
        assert loaded is not None
        assert loaded.success is None
        assert loaded.duration_ms is None

        recovery.success = False
        recovery.duration_ms = 30
        recovery.ended_at = BASE_TIME + timedelta(milliseconds=30)
        aer.recoveries.update(recovery)

        reloaded = aer.recoveries.get("rec-1")
        assert reloaded is not None
        assert reloaded.success is False
        assert reloaded.duration_ms == 30

    def test_update_of_an_unknown_recovery_raises(self, aer: AER) -> None:
        seed_run(aer)

        with pytest.raises(RecordNotFoundError, match="Recovery not found"):
            aer.recoveries.update(make_recovery("ghost"))

    def test_requires_an_existing_run(self, aer: AER) -> None:
        with pytest.raises(StorageError):
            aer.recoveries.create(make_recovery(run_id="missing"))

    def test_requires_an_existing_error_when_linked(self, aer: AER) -> None:
        seed_run(aer)

        with pytest.raises(StorageError):
            aer.recoveries.create(make_recovery(error_id="missing-error"))

    def test_get_by_run_and_by_error(self, aer: AER) -> None:
        seed_run(aer, "run-1")
        seed_run(aer, "run-2")
        aer.errors.create(make_error("err-1"))

        aer.recoveries.create(make_recovery("r1", error_id="err-1"))
        aer.recoveries.create(
            make_recovery("r2", run_id="run-2", started_at=BASE_TIME + timedelta(minutes=1))
        )

        assert [r.id for r in aer.recoveries.get_by_run("run-1")] == ["r1"]
        assert [r.id for r in aer.recoveries.get_by_run("run-2")] == ["r2"]
        assert [r.id for r in aer.recoveries.get_by_error("err-1")] == ["r1"]
        assert aer.recoveries.get_by_error("no-such-error") == []

    def test_count_by_run(self, aer: AER) -> None:
        seed_run(aer)
        aer.recoveries.create(make_recovery("r1"))
        aer.recoveries.create(make_recovery("r2", started_at=BASE_TIME + timedelta(minutes=1)))

        assert aer.recoveries.count_by_run("run-1") == 2
        assert aer.recoveries.count_by_run("run-2") == 0
