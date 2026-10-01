"""The DSH adapter against the real AER runtime.

These tests exercise the Python half of the integration end to end -- a real ingestor,
real repositories, a real runtime -- with the TypeScript half replaced by the payloads
it sends. That substitution is the point of the bridge protocol: the two sides agree on
a small vocabulary, so "what DSH would have sent" is a dictionary literal here.

What is deliberately *not* tested here: whether DSH emits these events. That is the
plugin's own test suite and the captured fixtures' job.
"""

from __future__ import annotations

from typing import Any

import pytest

from aer.adapter.dsh.adapter import (
    ADAPTER_NAME,
    DSH_TURN_OUTCOMES,
    DshAdapter,
    RuntimeSink,
    dsh_capabilities,
)
from aer.runtime.enums import EventType, RunStatus

SESSION = {"external_session_id": "dsh-session-1", "cwd": "/workspace", "agent_preset": None}


def envelope(
    event_type: EventType,
    *,
    seq: int,
    turn: int = 1,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """One normalized envelope, as the plugin sends it."""
    return {
        "event_type": event_type.value,
        "external_event_id": event_id or f"dsh-session-1:{seq}",
        "external_session_id": "dsh-session-1",
        "external_turn_id": str(turn),
        "external_sequence": seq,
        "external_timestamp": "2026-09-28T08:00:00+00:00",
        "payload": payload or {},
        "metadata": {"dsh": {"event": "tool/call", "seq": seq, "turn": turn}},
    }


def tool_call(
    seq: int,
    *,
    name: str = "bash",
    arguments: str = '{"cmd":"echo hi"}',
) -> dict[str, Any]:
    """A `tool/call` envelope."""
    return envelope(
        EventType.TOOL_CALL,
        seq=seq,
        payload={"tool": name, "call_id": "call-1", "arguments": arguments, "step": 0},
    )


def tool_result(
    seq: int,
    *,
    success: bool,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `tool/result` envelope carrying DSH's structured outcome."""
    payload: dict[str, Any] = {
        "tool": "call-1",
        "call_id": "call-1",
        "success": success,
        "result": "AER-E2E-OK" if success else "command not found",
        "step": 0,
    }
    if error is not None:
        payload["error"] = error
    return envelope(EventType.TOOL_RESULT, seq=seq, payload=payload)


@pytest.fixture
def sink(ingestor) -> RuntimeSink:
    """A runtime sink bound to the real ingestor."""
    return RuntimeSink(ingestor=ingestor, adapter=DshAdapter(provider="openai-codex"))


def open_turn(sink: RuntimeSink, *, turn: int = 1, task: str = "run the tests") -> Any:
    """Open the run for one turn, the way `turn.begin` does."""
    return sink.turn_begin({"session": SESSION, "turn": turn, "task": task, "model": None})


# -- identity --------------------------------------------------------------


def test_harness_and_provider_are_recorded_separately():
    """DSH is the harness; the model provider is whatever the runtime resolved."""
    identity = DshAdapter(provider="openai-codex", model="gpt-6-sol").identity()

    assert identity.agent_name == "dsh"
    assert identity.provider == "openai-codex"
    assert identity.adapter_name == ADAPTER_NAME
    # A rule keyed on the model would silently stop matching when it is renamed.
    assert identity.model == "gpt-6-sol"


def test_provider_is_never_assumed_from_the_harness_name():
    """The default provider is visibly unknown, not a guess at DeepSeek."""
    assert DshAdapter().identity().provider == "unknown"


def test_capabilities_declare_injection_but_not_adoption():
    """The one claim DSH earns, and the ones it does not."""
    capabilities = dsh_capabilities()

    assert capabilities.context_injection is True
    assert capabilities.tool_events is True
    assert capabilities.session_linkage is True
    assert capabilities.explicit_adoption_signal is False
    assert capabilities.explicit_utility_signal is False
    assert capabilities.external_verification is False


# -- run granularity -------------------------------------------------------


def test_one_turn_is_one_run(sink: RuntimeSink):
    """A turn opens exactly one run (round-8.2 section 11)."""
    result = open_turn(sink, turn=1, task="first task")

    assert result["run_id"]
    assert result["outcome"] == "STARTED"


def test_a_second_turn_opens_a_second_run_not_a_resume(sink: RuntimeSink):
    """One DSH session maps to many AER runs (section 13)."""
    first = open_turn(sink, turn=1, task="first task")
    sink.turn_end(
        {
            "session": SESSION,
            "turn": 1,
            "reason_kind": "completed",
            "outcome": "INCONCLUSIVE",
            "detail": {},
        }
    )
    second = open_turn(sink, turn=2, task="second task")

    assert second["run_id"] != first["run_id"], "a second turn must not resume the first run"


def test_events_go_to_the_run_of_their_turn(sink: RuntimeSink):
    """A tool call is attributed to the run its turn opened."""
    opened = open_turn(sink)
    applied = sink.event_ingest({"envelope": tool_call(3)})

    assert applied["applied"] is True
    assert applied["run_id"] == opened["run_id"]


def test_an_event_without_an_open_turn_is_refused_not_guessed(sink: RuntimeSink):
    """An unattributable event is dropped rather than attached to some other run."""
    result = sink.event_ingest({"envelope": tool_call(3)})

    assert result["applied"] is False
    assert result["reason"] == "no open turn for this session"


# -- turn outcome ----------------------------------------------------------


def test_completed_is_not_success(sink: RuntimeSink):
    """`completed` means the loop stopped, not that the task succeeded (section 16)."""
    open_turn(sink)
    result = sink.turn_end(
        {
            "session": SESSION,
            "turn": 1,
            "reason_kind": "completed",
            "outcome": "INCONCLUSIVE",
            "detail": {},
        }
    )

    assert result["closed"] is True
    assert result["status"] == RunStatus.INCONCLUSIVE.value


def test_a_structured_error_closes_the_run_as_failed(sink: RuntimeSink):
    """DSH's `error` reason is a declaration about the work, so it is one here."""
    open_turn(sink)
    result = sink.turn_end(
        {
            "session": SESSION,
            "turn": 1,
            "reason_kind": "error",
            "outcome": "FAILED",
            "detail": {"message": "boom", "code": "UNKNOWN"},
        }
    )

    assert result["status"] == RunStatus.FAILED.value


def test_an_unknown_reason_closes_without_claiming_anything(sink: RuntimeSink):
    """`TurnEndReasonMap` is merge-extensible; a new variant must not hang the run."""
    open_turn(sink)
    result = sink.turn_end(
        {
            "session": SESSION,
            "turn": 1,
            "reason_kind": "something-new",
            "outcome": "INCONCLUSIVE",
            "detail": {},
        }
    )

    assert result["status"] == RunStatus.INCONCLUSIVE.value


def test_every_documented_reason_has_a_status():
    """The mapping covers the vocabulary DSH actually publishes."""
    assert set(DSH_TURN_OUTCOMES) == {
        "completed",
        "aborted",
        "blocked",
        "error",
        "max-tokens",
        "interrupted",
    }
    assert DSH_TURN_OUTCOMES["aborted"] == RunStatus.ABORTED
    assert DSH_TURN_OUTCOMES["error"] == RunStatus.FAILED
    # The rest state nothing about the work.
    for kind in ("completed", "blocked", "max-tokens", "interrupted"):
        assert DSH_TURN_OUTCOMES[kind] == RunStatus.INCONCLUSIVE


# -- structured tool outcomes ----------------------------------------------


def test_a_successful_tool_result_is_recorded_as_success(sink: RuntimeSink):
    """`isError: false` is DSH's own statement, not an inference from the text."""
    open_turn(sink)
    result = sink.event_ingest({"envelope": tool_result(4, success=True)})

    assert result["applied"] is True


def test_a_failed_tool_result_is_recorded_as_failure(sink: RuntimeSink):
    """DSH publishes the failure structurally, so AER records it as one."""
    open_turn(sink)
    result = sink.event_ingest(
        {
            "envelope": tool_result(
                4,
                success=False,
                error={"error_type": "ToolError.UNKNOWN_TOOL", "message": "no such tool"},
            )
        }
    )

    assert result["applied"] is True


def test_a_tool_result_drops_reasoning_but_keeps_the_outcome(sink: RuntimeSink):
    """Private reasoning never reaches AER; the structured outcome still does (section 77)."""
    adapter = DshAdapter(provider="openai-codex")

    translated = adapter.handle_event(
        {
            "event_type": EventType.TOOL_RESULT.value,
            "external_event_id": "s:4",
            "external_session_id": "s",
            "payload": {"tool": "c1", "call_id": "c1", "success": False, "result": "denied"},
            "metadata": {},
        }
    )

    assert len(translated) == 1
    assert translated[0].observation is not None
    assert translated[0].observation.kind == "TOOL_FAILURE"


def test_an_event_type_the_adapter_does_not_translate_is_dropped():
    """Untranslatable envelopes are dropped by name, not coerced."""
    adapter = DshAdapter()

    assert adapter.handle_event({"event_type": "HUMAN_FEEDBACK"}) == ()


# -- idempotency -----------------------------------------------------------


def test_the_same_event_delivered_twice_produces_one_event(sink: RuntimeSink):
    """One `session_id + seq` is one AER event (sections 41-42)."""
    open_turn(sink)
    first = sink.event_ingest({"envelope": tool_call(3)})
    second = sink.event_ingest({"envelope": tool_call(3)})

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["outcome"] == "DUPLICATE"


# -- retrieval and injection -----------------------------------------------


def test_retrieval_without_injection_records_nothing_as_injected(sink: RuntimeSink):
    """Retrieved but never injected is a distinguishable state (sections 29, 72)."""
    open_turn(sink)
    offered = sink.retrieve({"session": SESSION, "turn": 1, "task": "run the tests"})

    # No experience in a fresh knowledge base, so there is nothing to offer -- and
    # nothing was recorded as injected either way.
    assert offered["experience_count"] == 0
    assert sink.injected(
        {
            "session": SESSION,
            "turn": 1,
            "retrieval_id": offered["retrieval_id"],
            "experience_count": 0,
            "seq": 5,
        }
    ) == {"recorded": False, "reason": "no pending retrieval for this session"}


def test_an_injection_without_a_pending_retrieval_is_refused(sink: RuntimeSink):
    """A usage record needs a retrieval to belong to."""
    open_turn(sink)

    result = sink.injected(
        {"session": SESSION, "turn": 1, "retrieval_id": "none", "experience_count": 1, "seq": 5}
    )

    assert result["recorded"] is False


def test_the_sink_counts_what_the_plugin_delivered(sink: RuntimeSink):
    """Counts are how a probe tells talking from delivering."""
    from aer.adapter.dsh.protocol import BridgeRequest

    sink.observe(BridgeRequest(protocol=1, request_id="a", operation="turn.begin"), None)
    sink.observe(BridgeRequest(protocol=1, request_id="b", operation="turn.begin"), None)
    sink.observe(BridgeRequest(protocol=1, request_id="c", operation="event.ingest"), None)

    assert sink.counts == {"turn.begin": 2, "event.ingest": 1}
