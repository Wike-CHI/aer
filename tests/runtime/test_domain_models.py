"""Domain model and enum tests (Milestone 1, Tasks 1.1 / 1.2 / 1.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aer import Event, EventType, Run, RunStatus, VerifierType
from aer.runtime.sanitization import FALLBACK_REPR_MAX_LENGTH, REDACTED
from aer.runtime.serialization import (
    FALLBACK_MARKER_KEY,
    is_fallback_marker,
    to_json_value,
    utc_now,
)


class TestRunStatus:
    def test_exposes_only_the_agreed_vocabulary(self) -> None:
        assert {status.value for status in RunStatus} == {
            "RUNNING",
            "SUCCESS",
            "PARTIAL_SUCCESS",
            "FAILED",
            "ABORTED",
        }

    def test_members_are_plain_strings(self) -> None:
        assert str(RunStatus.SUCCESS) == "SUCCESS"
        assert f"{RunStatus.FAILED}" == "FAILED"

    def test_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Run(task_description="task", status="NOT_A_STATUS")  # type: ignore[arg-type]

    def test_assignment_of_unknown_status_is_rejected(self) -> None:
        run = Run(task_description="task")
        with pytest.raises(ValidationError):
            run.status = "NOT_A_STATUS"  # type: ignore[assignment]


class TestEventType:
    def test_exposes_only_the_agreed_vocabulary(self) -> None:
        assert {event_type.value for event_type in EventType} == {
            "TASK_START",
            "MODEL_CALL",
            "MODEL_RESULT",
            "TOOL_CALL",
            "TOOL_RESULT",
            "ERROR",
            "RECOVERY_START",
            "RECOVERY_RESULT",
            "VERIFICATION",
            "HUMAN_FEEDBACK",
            "TASK_END",
        }

    def test_unknown_event_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(run_id="run-1", event_type="NOPE")  # type: ignore[arg-type]


class TestVerifierType:
    def test_matches_the_documented_trust_order(self) -> None:
        """Declaration order *is* the trust order, most trustworthy first.

        Nothing sorts verifiers today, but a future ranker or confidence score will,
        and an enum whose order disagrees with its docstring is a trap. Agent
        self-evaluation is absent on purpose: an agent's own claim is not
        verification (TASKS.md #27.7).
        """
        assert [member.value for member in VerifierType] == [
            "DETERMINISTIC",
            "ENVIRONMENT",
            "HUMAN",
            "LLM",
        ]


class TestRun:
    def test_defaults(self) -> None:
        run = Run(task_description="Fix WordPress H1")

        assert run.status is RunStatus.RUNNING
        assert run.ended_at is None
        assert run.final_score is None
        assert run.metadata == {}
        assert run.id
        assert run.is_finished is False

    def test_started_at_is_timezone_aware_utc(self) -> None:
        run = Run(task_description="task")

        assert run.started_at.tzinfo is not None
        assert run.started_at.utcoffset() == UTC.utcoffset(None)

    def test_naive_started_at_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Run(task_description="task", started_at=datetime(2026, 1, 1))

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Run(task_description="task", typo_field="x")  # type: ignore[call-arg]

    def test_non_json_metadata_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Run(task_description="task", metadata={"bad": object()})  # type: ignore[dict-item]

    def test_terminal_status_marks_the_run_as_finished(self) -> None:
        run = Run(task_description="task", status=RunStatus.ABORTED)

        assert run.is_finished is True


class TestEvent:
    def test_defaults(self) -> None:
        event = Event(run_id="run-1", event_type=EventType.ERROR)

        assert event.id is None
        assert event.sequence is None
        assert event.input is None
        assert event.output is None
        assert event.duration_ms is None
        assert event.is_persisted is False
        assert event.created_at.tzinfo is not None

    def test_sequence_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Event(run_id="run-1", event_type=EventType.ERROR, sequence=0)

    def test_negative_duration_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(run_id="run-1", event_type=EventType.TOOL_CALL, duration_ms=-1)

    def test_nested_json_payload_is_accepted(self) -> None:
        event = Event(
            run_id="run-1",
            event_type=EventType.TOOL_RESULT,
            output={"status": 200, "items": [1, "two", None, {"deep": True}]},
        )

        assert event.output == {"status": 200, "items": [1, "two", None, {"deep": True}]}


class TestSerialization:
    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().tzinfo is not None

    def test_known_types_are_converted(self) -> None:
        payload = {
            "when": datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
            "status": RunStatus.SUCCESS,
            "pair": (1, 2),
            "nested": {"items": [None, True, 1.5]},
        }

        assert to_json_value(payload) == {
            "when": "2026-09-15T12:00:00+00:00",
            "status": "SUCCESS",
            "pair": [1, 2],
            "nested": {"items": [None, True, 1.5]},
        }

    def test_pydantic_models_are_converted_to_structures(self) -> None:
        converted = to_json_value(Event(run_id="run-1", event_type=EventType.ERROR))

        assert isinstance(converted, dict)
        assert converted["run_id"] == "run-1"
        assert converted["event_type"] == "ERROR"
        assert converted["metadata"] == {}
        created_at = str(converted["created_at"])
        assert created_at.startswith("2026-") and "T" in created_at

    def test_dataclasses_are_converted_to_structures(self) -> None:
        @dataclass
        class Payload:
            name: str
            count: int

        assert to_json_value(Payload(name="x", count=2)) == {"name": "x", "count": 2}

    def test_unknown_objects_become_an_explicit_marker(self) -> None:
        """Not a silent ``str(obj)``: a degraded payload has to stay findable."""

        class Opaque:
            pass

        converted = to_json_value({"opaque": Opaque()})

        assert isinstance(converted, dict)
        marker = converted["opaque"]
        assert isinstance(marker, dict)
        assert marker[FALLBACK_MARKER_KEY] is True
        assert str(marker["python_type"]).endswith(".Opaque")
        assert "Opaque object at" in str(marker["repr"])
        assert is_fallback_marker(marker)

    def test_bytes_do_not_embed_binary_content(self) -> None:
        converted = to_json_value(b"\x00\x01\x02")

        assert isinstance(converted, dict)
        assert converted[FALLBACK_MARKER_KEY] is True
        assert converted["python_type"] == "builtins.bytes"

    def test_fallback_repr_is_redacted_and_truncated(self) -> None:
        class Leaky:
            def __repr__(self) -> str:
                return f"Leaky(api_key=SECRETVALUE123, padding={'x' * 8000})"

        converted = to_json_value(Leaky())

        assert isinstance(converted, dict)
        rendered = str(converted["repr"])
        assert "SECRETVALUE123" not in rendered
        assert REDACTED in rendered
        assert "truncated" in rendered
        # The elision marker is counted inside the cap, so the limit is exact.
        assert len(rendered) <= FALLBACK_REPR_MAX_LENGTH

    def test_a_broken_repr_does_not_lose_the_trace(self) -> None:
        class Broken:
            def __repr__(self) -> str:
                raise ValueError("repr is broken")

        converted = to_json_value(Broken())

        assert isinstance(converted, dict)
        assert converted[FALLBACK_MARKER_KEY] is True
        assert "repr() failed" in str(converted["repr"])

    def test_plain_containers_are_not_marked(self) -> None:
        assert not is_fallback_marker({"plain": "dict"})
        assert not is_fallback_marker([1, 2, 3])
