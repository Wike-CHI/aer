"""Translating protocol events into AER run events (sections 22-23, 28, 39-40, 47-49).

Two things are being checked throughout. First, that the mapping preserves what the
source actually said -- a failure is a failure, a recovery is a recovery, and neither is
inferred from the other. Second, that nothing an integration can send is able to skip a
guard: not the terminal-state rule, not the verification engine, and not the capability
declaration it made at registration.
"""

from __future__ import annotations

import pytest

from aer import AER, GenericAgentAdapter, UsageSignal, UtilityLabel
from aer.adapter import AdapterIngestor
from aer.exceptions import (
    AdapterCapabilityError,
    AdapterError,
    AdapterProtocolError,
    RunStateError,
    UnsupportedAdapterEvent,
)
from aer.runtime.enums import EventType
from tests.adapters.support import (
    CrashingAdapter,
    ShellAgent,
    ShellStyleAdapter,
    StructuredAgent,
    StructuredStyleAdapter,
)

SESSION = {"session_id": "s1", "task": "fix the REST API 403"}


class TestEventMapping:
    def test_a_tool_call_becomes_a_tool_call(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        result = ingestor.ingest(
            handle, {"type": "tool_call", "tool": "shell", "input": "pytest -q", "event_id": "e1"}
        )

        event = adapter_runtime.get_events(handle.run_id)[-1]
        assert event.event_type is EventType.TOOL_CALL
        assert result.aer_event_id == event.id
        assert event.metadata["source"] == "adapter:aer-generic"
        assert event.metadata["provider"] == "generic"

    def test_a_failed_tool_result_writes_an_error_before_the_result(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The trace reads CALL -> ERROR -> RESULT, exactly as the in-process path does."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

        ingestor.ingest(
            handle,
            {
                "type": "tool_result",
                "tool": "shell",
                "success": False,
                "error": {"error_type": "shell.NonZeroExit", "message": "2 failed"},
                "event_id": "e2",
            },
        )

        types = [event.event_type.value for event in adapter_runtime.get_events(handle.run_id)]
        assert types == ["TASK_START", "TOOL_CALL", "ERROR", "TOOL_RESULT"]
        errors = adapter_runtime.get_errors(handle.run_id)
        assert len(errors) == 1
        assert errors[0].error_type == "shell.NonZeroExit"
        assert errors[0].error_message == "2 failed"

    def test_a_successful_tool_result_writes_no_error(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.ingest(
            handle,
            {
                "type": "tool_result",
                "tool": "shell",
                "success": True,
                "result": "ok",
                "event_id": "e1",
            },
        )

        assert adapter_runtime.get_errors(handle.run_id) == []

    def test_a_standalone_error_carries_the_sources_own_type_and_stack(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 28: AER records what the source said, not a traceback of its own."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.ingest(
            handle,
            {
                "type": "error",
                "error_type": "builtins.TimeoutError",
                "message": "the request timed out after 30s",
                "stack_trace": "at wp.update (client.ts:41)",
                "event_id": "e1",
            },
        )

        (record,) = adapter_runtime.get_errors(handle.run_id)
        assert record.error_type == "builtins.TimeoutError"
        assert record.error_message == "the request timed out after 30s"
        assert record.stack_trace == "at wp.update (client.ts:41)"

    def test_an_error_with_no_description_is_recorded_as_undescribed(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """A cause AER invented would be worse than a message that admits ignorance."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.ingest(handle, {"type": "error", "error_type": "external.Boom", "event_id": "e1"})

        (record,) = adapter_runtime.get_errors(handle.run_id)
        assert "without describing it" in record.error_message


class TestRecovery:
    def test_a_repair_attempt_becomes_a_recovery_record(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.ingest(
            handle,
            {"type": "error", "error_type": "http.403", "message": "forbidden", "event_id": "e1"},
        )
        (error,) = adapter_runtime.get_errors(handle.run_id)

        ingestor.ingest(
            handle,
            {
                "type": "recovery_start",
                "reason": "grant the capability",
                "error_id": error.id,
                "event_id": "e2",
            },
        )
        open_records = adapter_runtime.get_recoveries(handle.run_id)
        assert len(open_records) == 1
        assert open_records[0].success is None

        ingestor.ingest(handle, {"type": "recovery_result", "success": True, "event_id": "e3"})

        (record,) = adapter_runtime.get_recoveries(handle.run_id)
        assert record.success is True
        assert record.reason == "grant the capability"
        assert adapter_runtime.errors.get(error.id).resolved is True
        assert record.result_event_id is not None

    def test_a_failed_repair_resolves_nothing(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.ingest(
            handle,
            {"type": "error", "error_type": "http.403", "message": "forbidden", "event_id": "e1"},
        )
        (error,) = adapter_runtime.get_errors(handle.run_id)
        ingestor.ingest(
            handle,
            {
                "type": "recovery_start",
                "reason": "clear the cache",
                "error_id": error.id,
                "event_id": "e2",
            },
        )

        ingestor.ingest(handle, {"type": "recovery_result", "success": False, "event_id": "e3"})

        assert adapter_runtime.errors.get(error.id).resolved is False
        assert adapter_runtime.get_recoveries(handle.run_id)[0].success is False

    def test_a_result_with_no_open_attempt_is_refused(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """An end without a beginning cannot be recorded as a repair history."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        with pytest.raises(AdapterProtocolError, match="no open recovery attempt"):
            ingestor.ingest(handle, {"type": "recovery_result", "success": True, "event_id": "e1"})

    def test_a_repair_attempt_needs_a_reason(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        with pytest.raises(AdapterProtocolError, match="non-empty 'reason'"):
            ingestor.ingest(handle, {"type": "recovery_start", "event_id": "e1"})

    def test_a_recovery_result_must_state_success(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.ingest(handle, {"type": "recovery_start", "reason": "try again", "event_id": "e1"})

        with pytest.raises(AdapterProtocolError, match="must state success"):
            ingestor.ingest(handle, {"type": "recovery_result", "event_id": "e2"})


class TestVerificationOwnership:
    def test_a_verification_envelope_is_refused(self) -> None:
        """Section 39: only the engine writes verdicts."""
        from aer.adapter import AgentExecutionEnvelope, AgentIdentity

        envelope = AgentExecutionEnvelope(
            event_type=EventType.VERIFICATION,
            identity=AgentIdentity(
                provider="p", agent_name="a", adapter_name="x", adapter_version="1"
            ),
        )

        with pytest.raises(UnsupportedAdapterEvent, match="verification engine"):
            envelope.require_ingestable()

    def test_relaying_evidence_to_a_real_verifier_is_allowed(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The supported route: the adapter supplies evidence, the engine judges it."""
        from aer import HttpStatusVerifier

        agent = StructuredAgent(ingestor, StructuredStyleAdapter())
        handle = agent.connect()
        agent.call("http.request", parameters={"url": "https://example.test/wp"})
        agent.outcome("http.request", ok=True, payload={"status": 200})
        agent.finish(outcome="success")

        verdict = ingestor.verify(
            handle,
            HttpStatusVerifier(expected_status=200, required=True),
            payload={"actual_status": 200},
        )

        assert verdict.passed is True
        assert verdict.verifier_type.value == "DETERMINISTIC"
        # The run is a verified success only because the engine recorded the verdict:
        # nothing the adapter said contributed to it (section 39).
        assert adapter_runtime.verified_success(handle.run_id) is True

    def test_verification_requires_the_capability(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        from aer import HttpStatusVerifier

        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        with pytest.raises(AdapterCapabilityError, match="external_verification"):
            ingestor.verify(
                handle,
                HttpStatusVerifier(expected_status=200),
                payload={"actual_status": 200},
            )


class TestCapabilityGates:
    def test_adoption_is_refused_when_the_adapter_cannot_observe_it(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Section 47: the reference adapter declares no adoption capability."""
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(adapter_runtime)
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        tracked = retrieve(adapter_runtime, index, guidance=[hit], run_id=handle.run_id)

        with pytest.raises(AdapterCapabilityError, match="explicit_adoption_signal"):
            ingestor.record_usage_signal(
                handle, tracked, experience_id="exp-1", signal=UsageSignal.ADOPTED
            )

        stored = adapter_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.usage_signal is UsageSignal.UNKNOWN
        assert stored.usage_signal_source is None

    def test_utility_is_refused_without_its_own_capability(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Sections 16 and 34: a successful run must not become a HELPFUL label."""
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(adapter_runtime)
        agent = StructuredAgent(ingestor, StructuredStyleAdapter())
        handle = agent.connect()
        tracked = retrieve(adapter_runtime, index, guidance=[hit], run_id=handle.run_id)

        with pytest.raises(AdapterCapabilityError, match="explicit_utility_signal"):
            ingestor.record_utility(
                handle, tracked, experience_id="exp-1", label=UtilityLabel.HELPFUL
            )

        stored = adapter_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.utility_label is UtilityLabel.UNKNOWN

    def test_tool_events_are_refused_without_the_capability(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        from aer import AdapterCapabilities

        handle = ingestor.open(GenericAgentAdapter(capabilities=AdapterCapabilities()), SESSION)

        with pytest.raises(AdapterCapabilityError, match="tool_events"):
            ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

    def test_human_feedback_requires_the_capability(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        with pytest.raises(AdapterCapabilityError, match="human_feedback"):
            ingestor.ingest(handle, {"type": "human_feedback", "approved": True, "event_id": "e1"})

    def test_a_payload_the_adapter_does_not_understand_is_an_adapter_error(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """A generic payload sent to the structured adapter is a wiring mistake."""
        handle = StructuredAgent(ingestor, StructuredStyleAdapter()).connect()

        with pytest.raises(UnsupportedAdapterEvent, match="does not know the phase"):
            ingestor.ingest(handle, {"type": "human_feedback", "approved": True})

    def test_adoption_is_recorded_when_the_capability_is_declared(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(adapter_runtime)
        agent = StructuredAgent(ingestor, StructuredStyleAdapter())
        handle = agent.connect()
        tracked = retrieve(adapter_runtime, index, guidance=[hit], run_id=handle.run_id)

        ingestor.record_usage_signal(
            handle, tracked, experience_id="exp-1", signal=UsageSignal.ADOPTED
        )

        stored = adapter_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.usage_signal is UsageSignal.ADOPTED
        assert stored.usage_signal_source.value == "ADAPTER"

    def test_usage_for_another_sessions_retrieval_is_refused(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Cross-wired sessions would otherwise produce an unattributable fact."""
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(adapter_runtime)
        first = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="one").connect()
        second = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="two").connect()
        tracked = retrieve(adapter_runtime, index, guidance=[hit], run_id=first.run_id)

        with pytest.raises(AdapterProtocolError, match="belongs to run"):
            ingestor.inject(second, tracked)

    def test_human_feedback_is_recorded_when_declared(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        agent = StructuredAgent(ingestor, StructuredStyleAdapter())
        handle = agent.connect()

        agent.annotate(approved=True, note="looks right")

        feedback = [
            event
            for event in adapter_runtime.get_events(handle.run_id)
            if event.event_type is EventType.HUMAN_FEEDBACK
        ]
        assert len(feedback) == 1
        assert feedback[0].output["approved"] is True


class TestOrdering:
    def test_a_late_arrival_keeps_its_arrival_order_and_its_own_numbering(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 49: AER's sequence is arrival order; the vendor's is kept beside it."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        for event_id, sequence in (("e3", 3), ("e1", 1), ("e2", 2)):
            ingestor.ingest(
                handle,
                {
                    "type": "tool_call",
                    "tool": "shell",
                    "sequence": sequence,
                    "event_id": event_id,
                },
            )

        events = adapter_runtime.get_events(handle.run_id)
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert [event.metadata["external"]["sequence"] for event in events[1:]] == [3, 1, 2]

    def test_the_ledger_keeps_the_external_timestamp(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.ingest(
            handle,
            {
                "type": "tool_call",
                "tool": "shell",
                "sequence": 1,
                "timestamp": "2026-09-20T09:00:00+00:00",
                "event_id": "e1",
            },
        )

        entry = adapter_runtime.adapter_events.find("generic", "e1")
        assert entry is not None
        assert entry.external_sequence == 1
        assert entry.external_timestamp is not None
        assert entry.external_timestamp.hour == 9

    def test_a_naive_vendor_timestamp_is_dropped_rather_than_assumed_local(self) -> None:
        adapter = GenericAgentAdapter()

        envelope = adapter.handle_event(
            {"type": "tool_call", "tool": "shell", "timestamp": "2026-09-20T09:00:00"}
        )[0]

        assert envelope.external_timestamp is None

    def test_aers_own_timestamp_is_the_receive_clock(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 24: an Agent with a wrong clock cannot move events in the timeline."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.ingest(
            handle,
            {
                "type": "tool_call",
                "tool": "shell",
                "timestamp": "1999-01-01T00:00:00+00:00",
                "event_id": "e1",
            },
        )

        event = adapter_runtime.get_events(handle.run_id)[-1]
        assert event.created_at.year >= 2026
        assert event.metadata["external"]["timestamp"].startswith("1999")


class TestTerminalGuard:
    def test_human_feedback_may_arrive_after_the_run_ended(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """A statement *about* a finished run is an observation, not a mutation."""
        agent = StructuredAgent(ingestor, StructuredStyleAdapter())
        handle = agent.connect()
        agent.finish(outcome="success")

        agent.annotate(approved=True, note="good")

        types = [event.event_type.value for event in adapter_runtime.get_events(handle.run_id)]
        assert types == ["TASK_START", "TASK_END", "HUMAN_FEEDBACK"]

    def test_a_tool_result_after_the_run_ended_is_refused(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.close(handle, {"status": "success"})

        with pytest.raises(RunStateError, match="already finished"):
            ingestor.ingest(
                handle,
                {
                    "type": "tool_result",
                    "tool": "shell",
                    "success": True,
                    "event_id": "late",
                },
            )


class TestAdapterCrash:
    def test_a_crashing_translation_does_not_touch_the_run(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 48: an integration bug must not look like an agent failure."""
        handle = ingestor.open(CrashingAdapter(), SESSION)
        before = adapter_runtime.get_events(handle.run_id)

        with pytest.raises(AdapterError) as raised:
            ingestor.ingest(handle, {"type": "explode"})

        assert isinstance(raised.value.__cause__, RuntimeError)
        assert "not modified" in str(raised.value)
        assert adapter_runtime.get_events(handle.run_id) == before
        assert adapter_runtime.get_errors(handle.run_id) == []
        assert adapter_runtime.get_verifications(handle.run_id) == []
        assert adapter_runtime.get_run(handle.run_id).status.value == "RUNNING"

    def test_nothing_is_claimed_for_a_translation_that_crashed(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """So the platform's retry is not mistaken for a duplicate."""
        handle = ingestor.open(CrashingAdapter(), SESSION)

        with pytest.raises(AdapterError):
            ingestor.ingest(handle, {"type": "explode", "event_id": "e1"})

        assert adapter_runtime.adapter_events.count() == 0

    def test_the_session_is_still_usable_afterwards(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(CrashingAdapter(), SESSION)
        with pytest.raises(AdapterError):
            ingestor.ingest(handle, {"type": "explode"})

        result = ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

        assert result.applied is True
        run = adapter_runtime.get_run(handle.run_id)
        assert run.status.value == "RUNNING"


class TestTwoVocabulariesReachTheSamePlace:
    def test_a_shell_agent_and_a_structured_agent_produce_the_same_trace_shape(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 60 in miniature: different words, identical AER semantics."""
        shell = ShellAgent(ingestor, ShellStyleAdapter(), session_id="shell-1")
        shell_handle = shell.connect()
        shell.run("pytest -q", exit_code=0, stdout="4 passed")

        structured = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="struct-1")
        structured_handle = structured.connect()
        structured.call("pytest", parameters={"args": ["-q"]})
        structured.outcome("pytest", ok=True, payload="4 passed")

        shell_types = [
            event.event_type.value for event in adapter_runtime.get_events(shell_handle.run_id)
        ]
        structured_types = [
            event.event_type.value for event in adapter_runtime.get_events(structured_handle.run_id)
        ]
        assert shell_types[0] == structured_types[0] == "TASK_START"
        assert shell_types[1:] == ["TOOL_CALL", "TOOL_RESULT"]
        assert structured_types[1:] == ["TOOL_CALL", "TOOL_RESULT"]

        shell_body = adapter_runtime.get_events(shell_handle.run_id)[-1].output
        structured_body = adapter_runtime.get_events(structured_handle.run_id)[-1].output
        assert shell_body["success"] is True
        assert structured_body["success"] is True

    def test_a_failure_looks_the_same_from_both(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        shell = ShellAgent(ingestor, ShellStyleAdapter(), session_id="shell-1")
        shell_handle = shell.connect()
        shell.run("pytest -q", exit_code=1, stderr="2 failed")

        structured = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="struct-1")
        structured_handle = structured.connect()
        structured.call("pytest")
        structured.outcome("pytest", ok=False)

        for handle in (shell_handle, structured_handle):
            types = [event.event_type.value for event in adapter_runtime.get_events(handle.run_id)]
            assert types == ["TASK_START", "TOOL_CALL", "ERROR", "TOOL_RESULT"]
            (record,) = adapter_runtime.get_errors(handle.run_id)
            assert record.recoverable is True
