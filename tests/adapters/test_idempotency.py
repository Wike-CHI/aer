"""Idempotency: the same delivery twice is one event (sections 18-19, 42).

Real hooks retry. A webhook is delivered twice. A session replays. None of that is a
bug in the integration, and all of it would corrupt the trace if delivery counted as
occurrence -- a doubled tool call inflates the failure statistics that the experience
layer later learns from.
"""

from __future__ import annotations

import pytest

from aer import AER, AdapterIngestOutcome, GenericAgentAdapter
from aer.adapter import AdapterIngestor, AdapterRegistry
from aer.exceptions import AdapterProtocolError
from aer.runtime.enums import EventType
from tests.adapters.support import SloppyAdapter


def deliver(
    ingestor: AdapterIngestor,
    runtime: AER,
    session_id: str,
    *,
    event_id: str | None,
    tool: str = "shell",
    **adapter: object,
):
    handle = ingestor.open(
        GenericAgentAdapter(**adapter),  # type: ignore[arg-type]
        {"session_id": session_id, "task": "fix the 403"},
    )
    payload: dict[str, object] = {"type": "tool_call", "tool": tool, "input": "pytest -q"}
    if event_id is not None:
        payload["event_id"] = event_id
    return handle, ingestor.ingest(handle, payload)


class TestDuplicateDelivery:
    def test_the_same_external_event_is_applied_once(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        _, first = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")
        _, second = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")

        assert first.outcome is AdapterIngestOutcome.APPLIED
        assert second.outcome is AdapterIngestOutcome.DUPLICATE
        assert second.applied is False

    def test_the_run_carries_exactly_one_tool_call(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle, _ = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")
        ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "evt-1"})

        calls = [
            event
            for event in adapter_runtime.get_events(handle.run_id)
            if event.event_type is EventType.TOOL_CALL
        ]
        assert len(calls) == 1
        assert adapter_runtime.adapter_events.count(aer_run_id=handle.run_id) == 1

    def test_the_duplicate_reports_the_original_event_type(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        _, first = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")
        _, second = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")

        assert second.event_type is first.event_type
        assert second.external_event_id == "evt-1"

    def test_a_retry_storm_changes_nothing_further(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle, _ = deliver(ingestor, adapter_runtime, "s1", event_id="evt-1")
        raw = {"type": "tool_call", "tool": "shell", "event_id": "evt-1"}

        outcomes = {ingestor.ingest(handle, raw).outcome for _ in range(5)}

        assert outcomes == {AdapterIngestOutcome.DUPLICATE}
        assert adapter_runtime.adapter_events.count(aer_run_id=handle.run_id) == 1

    def test_an_event_without_an_id_is_applied_every_time(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The documented consequence of a platform that gives its events no identity.

        AER cannot deduplicate what it cannot identify, and pretending otherwise would
        mean inventing a key -- which would then be wrong the first time two genuinely
        distinct events looked alike.
        """
        handle, first = deliver(ingestor, adapter_runtime, "s1", event_id=None)
        _, second = deliver(ingestor, adapter_runtime, "s1", event_id=None)

        assert first.outcome is AdapterIngestOutcome.APPLIED
        assert second.outcome is AdapterIngestOutcome.APPLIED
        assert adapter_runtime.adapter_events.count(aer_run_id=handle.run_id) == 0


class TestBatchIdentity:
    def test_one_vendor_event_splitting_into_two_envelopes_is_still_one_event(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """A shell command becomes a call *and* a result, from one vendor delivery.

        The vendor's event is the unit of idempotency, so a retried delivery of that one
        event must not add a second call-and-result pair.
        """
        from tests.adapters.support import ShellAgent, ShellStyleAdapter

        agent = ShellAgent(ingestor, ShellStyleAdapter(), session_id="shell-1")
        handle = agent.connect()
        raw = {
            "kind": "exec",
            "cmd": "pytest -q",
            "exit_code": 0,
            "stdout": "ok",
            "seq": 1,
        }

        first = ingestor.ingest(handle, raw)
        second = ingestor.ingest(handle, raw)

        assert first.envelopes == 2
        assert first.applied is True
        assert second.outcome is AdapterIngestOutcome.DUPLICATE
        types = [event.event_type.value for event in adapter_runtime.get_events(handle.run_id)]
        assert types.count("TOOL_CALL") == 1
        assert types.count("TOOL_RESULT") == 1
        assert adapter_runtime.adapter_events.count(aer_run_id=handle.run_id) == 1

    def test_a_batch_records_how_many_envelopes_it_carried(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The ledger's event_type is the first applied type, so the count is recorded."""
        from tests.adapters.support import ShellAgent, ShellStyleAdapter

        agent = ShellAgent(ingestor, ShellStyleAdapter(), session_id="shell-1")
        handle = agent.connect()
        ingestor.ingest(handle, {"kind": "exec", "cmd": "pytest -q", "exit_code": 0, "seq": 4})

        entry = adapter_runtime.adapter_events.find("shell-agent", "4")
        assert entry is not None
        assert entry.metadata["envelopes"] == 2
        assert entry.metadata["event_types"] == ["TOOL_CALL", "TOOL_RESULT"]

    def test_envelopes_that_disagree_about_their_external_id_are_refused(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Picking one would make the ledger's guarantee meaningless (section 18)."""
        handle = ingestor.open(SloppyAdapter(), {"session_id": "s1", "task": "fix the 403"})

        with pytest.raises(AdapterProtocolError, match="different external_event_id"):
            ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "evt-1"})


class TestTheLedgerIsTheAuthority:
    def test_two_adapters_on_one_provider_cannot_both_claim_an_event(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The key is the vendor's namespace, so a second adapter is harmless.

        Registering two adapters for one provider is a misconfiguration. Keying on the
        provider means it degrades into "the event was already recorded" instead of
        silently doubling a trace (section 19).
        """
        first = GenericAgentAdapter(adapter_name="aer-generic-a")
        second = GenericAgentAdapter(adapter_name="aer-generic-b")
        handle_a = ingestor.open(first, {"session_id": "s1", "task": "fix the 403"})
        handle_b = ingestor.open(second, {"session_id": "s2", "task": "fix the 403"})

        applied = ingestor.ingest(
            handle_a, {"type": "tool_call", "tool": "shell", "event_id": "shared-1"}
        )
        duplicate = ingestor.ingest(
            handle_b, {"type": "tool_call", "tool": "shell", "event_id": "shared-1"}
        )

        assert applied.outcome is AdapterIngestOutcome.APPLIED
        assert duplicate.outcome is AdapterIngestOutcome.DUPLICATE
        assert duplicate.run_id == handle_b.run_id

    def test_the_ledger_records_what_the_event_became(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle, result = deliver(ingestor, adapter_runtime, "s1", event_id="evt-7")

        entry = adapter_runtime.adapter_events.find("generic", "evt-7")
        assert entry is not None
        assert entry.aer_run_id == handle.run_id
        assert entry.aer_event_id == result.aer_event_id
        assert entry.event_type is EventType.TOOL_CALL
        assert entry.adapter_name == "aer-generic"
        assert entry.is_applied is True

    def test_the_second_adapter_registration_needs_an_explicit_override(
        self, adapter_runtime: AER
    ) -> None:
        registry: AdapterRegistry = adapter_runtime.adapter_registry
        registry.register(GenericAgentAdapter)

        with pytest.raises(Exception, match="already registered"):
            registry.register(GenericAgentAdapter)

        registry.register(GenericAgentAdapter, replace=True)
        assert registry.names == ("aer-generic",)
