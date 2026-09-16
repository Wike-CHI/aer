"""VerificationRepository contracts (Milestone 4, sections 6, 7 and 37).

Exercised directly, without the engine, so each method's contract is pinned on its
own -- including the foreign keys, the cascade, and the refusal to guess at an
unknown ``verifier_type``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from aer import (
    AER,
    Event,
    EventType,
    Run,
    StorageError,
    VerificationRecord,
    VerifierType,
)
from aer.storage.models import RunRow

BASE_TIME = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def seed_run(aer: AER, run_id: str = "run-1") -> Run:
    return aer.runs.create(Run(id=run_id, task_description=f"task {run_id}", task_type="wordpress"))


def seed_event(aer: AER, run_id: str = "run-1") -> Event:
    return aer.events.create(Event(run_id=run_id, event_type=EventType.VERIFICATION))


def make_verification(verification_id: str = "ver-1", **overrides: object) -> VerificationRecord:
    payload: dict[str, object] = {
        "id": verification_id,
        "run_id": "run-1",
        "verifier_type": VerifierType.DETERMINISTIC,
        "verifier_name": "h1_count",
        "passed": False,
        "required": True,
        "score": 0.25,
        "message": "found 0 <h1> element(s), expected 1",
        "result": {"actual_count": 0, "expected_count": 1},
        "created_at": BASE_TIME,
        "metadata": {"sweep": "evening"},
    }
    payload.update(overrides)
    return VerificationRecord(**payload)  # type: ignore[arg-type]


class TestVerificationRepository:
    def test_round_trips_every_field(self, aer: AER) -> None:
        seed_run(aer)
        event = seed_event(aer)

        verification = make_verification(event_id=event.id)
        aer.verifications.create(verification)

        loaded = aer.verifications.get("ver-1")
        assert loaded is not None
        assert loaded == verification
        assert loaded.created_at.tzinfo is not None
        assert loaded.verifier_type is VerifierType.DETERMINISTIC
        assert loaded.passed is False
        assert loaded.required is True
        assert loaded.score == 0.25
        assert loaded.result == {"actual_count": 0, "expected_count": 1}
        assert loaded.metadata == {"sweep": "evening"}
        assert loaded.event_id == event.id

    def test_a_passing_verdict_round_trips_as_true(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(
            make_verification(passed=True, score=None, message=None, result={})
        )

        loaded = aer.verifications.get("ver-1")
        assert loaded is not None
        assert loaded.passed is True
        assert loaded.score is None
        assert loaded.message is None
        assert loaded.result == {}

    def test_required_defaults_to_true_in_the_model(self, aer: AER) -> None:
        seed_run(aer)
        bare = VerificationRecord(
            id="ver-bare",
            run_id="run-1",
            verifier_type=VerifierType.LLM,
            verifier_name="fluency",
            passed=True,
        )

        aer.verifications.create(bare)

        loaded = aer.verifications.get("ver-bare")
        assert loaded is not None
        assert loaded.required is True
        assert loaded.score is None
        assert loaded.result == {}

    def test_get_returns_none_for_an_unknown_id(self, aer: AER) -> None:
        assert aer.verifications.get("nope") is None

    def test_create_twice_with_the_same_id_fails(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(make_verification())

        with pytest.raises(StorageError):
            aer.verifications.create(make_verification())

    def test_requires_an_existing_run(self, aer: AER) -> None:
        with pytest.raises(StorageError):
            aer.verifications.create(make_verification(run_id="missing-run"))

    def test_requires_an_existing_event_when_linked(self, aer: AER) -> None:
        seed_run(aer)

        with pytest.raises(StorageError):
            aer.verifications.create(make_verification(event_id=9999))

    def test_event_id_is_optional(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(make_verification(event_id=None))

        loaded = aer.verifications.get("ver-1")
        assert loaded is not None
        assert loaded.event_id is None

    def test_get_by_run_is_ordered_and_scoped(self, aer: AER) -> None:
        seed_run(aer, "run-1")
        seed_run(aer, "run-2")
        aer.verifications.create(
            make_verification("v2", created_at=BASE_TIME + timedelta(minutes=1))
        )
        aer.verifications.create(make_verification("v1", created_at=BASE_TIME))
        aer.verifications.create(make_verification("other", run_id="run-2"))

        assert [v.id for v in aer.verifications.get_by_run("run-1")] == ["v1", "v2"]
        assert [v.id for v in aer.verifications.get_by_run("run-2")] == ["other"]
        assert aer.verifications.get_by_run("no-such-run") == []

    def test_get_by_run_filters_on_passed(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(make_verification("good", passed=True))
        aer.verifications.create(make_verification("bad", passed=False))

        assert [v.id for v in aer.verifications.get_by_run("run-1", passed=True)] == ["good"]
        assert [v.id for v in aer.verifications.get_by_run("run-1", passed=False)] == ["bad"]

    def test_get_by_run_filters_on_required(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(make_verification("mandatory", required=True))
        aer.verifications.create(make_verification("optional", required=False))

        assert [v.id for v in aer.verifications.get_by_run("run-1", required=True)] == ["mandatory"]
        assert [v.id for v in aer.verifications.get_by_run("run-1", required=False)] == ["optional"]

    def test_count_by_run(self, aer: AER) -> None:
        seed_run(aer)
        aer.verifications.create(make_verification("a", passed=True))
        aer.verifications.create(make_verification("b", passed=False, required=False))

        assert aer.verifications.count_by_run("run-1") == 2
        assert aer.verifications.count_by_run("run-1", passed=True) == 1
        assert aer.verifications.count_by_run("run-1", required=True) == 1
        assert aer.verifications.count_by_run("run-2") == 0

    def test_paging(self, aer: AER) -> None:
        seed_run(aer)
        for index in range(3):
            aer.verifications.create(
                make_verification(
                    f"v{index}",
                    created_at=BASE_TIME + timedelta(minutes=index),
                )
            )

        page = aer.verifications.get_by_run("run-1", limit=1, offset=1)
        assert [verification.id for verification in page] == ["v1"]

    def test_deleting_the_run_cascades_to_its_verdicts(self, aer: AER) -> None:
        """Section 37: a verdict must not outlive the run it describes."""
        seed_run(aer)
        aer.verifications.create(make_verification())

        with aer.database.session("delete run") as session:
            session.execute(delete(RunRow).where(RunRow.id == "run-1"))

        assert aer.verifications.get("ver-1") is None
        assert aer.verifications.count_by_run("run-1") == 0

    def test_an_unknown_stored_verifier_type_is_refused(self, aer: AER) -> None:
        """A value written by an incompatible version must fail loudly, not be guessed."""
        seed_run(aer)
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO verifications "
                "(id, run_id, verifier_type, verifier_name, passed, required, created_at) "
                "VALUES ('weird', 'run-1', 'TELEPATHY', 'psychic', 1, 1, "
                "'2026-09-16 12:00:00.000000')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="unknown value"):
            aer.verifications.get("weird")

    def test_a_corrupted_result_column_is_refused(self, aer: AER) -> None:
        seed_run(aer)
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO verifications "
                "(id, run_id, verifier_type, verifier_name, passed, required, result_json, "
                " created_at) "
                "VALUES ('broken', 'run-1', 'DETERMINISTIC', 'x', 1, 1, 'not json', "
                "'2026-09-16 12:00:00.000000')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="corrupted"):
            aer.verifications.get("broken")
