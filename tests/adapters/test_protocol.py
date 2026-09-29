"""The protocol surface: what it accepts, and what it refuses (section 41).

A protocol's value is in its refusals. Everything here is about the boundary: an unknown
event type, a version this build does not speak, a payload missing the field that makes
it interpretable. Each refusal exists because the alternative -- guessing, coercing or
dropping -- produces evidence that looks ordinary and is not.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aer import AER, AdapterCapabilities, AdapterError, AgentIdentity
from aer.adapter import (
    ADAPTER_EVENT_TYPES,
    AER_ADAPTER_PROTOCOL_VERSION,
    AdapterIngestor,
    AdapterRegistry,
    AgentAction,
    AgentExecutionEnvelope,
    AgentObservation,
    GenericAgentAdapter,
    ObservationKind,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType
from tests.adapters.support import FutureProtocolAdapter


class TestTheVocabularyIsClosed:
    def test_the_adapter_event_types_are_the_documented_subset(self) -> None:
        """Pinned so that widening the protocol is a deliberate edit (section 5)."""
        assert {item.value for item in ADAPTER_EVENT_TYPES} == {
            "MODEL_CALL",
            "MODEL_RESULT",
            "TOOL_CALL",
            "TOOL_RESULT",
            "ERROR",
            "RECOVERY_START",
            "RECOVERY_RESULT",
            "HUMAN_FEEDBACK",
        }

    def test_the_protocol_version_is_named_separately_from_the_package(self) -> None:
        """Section 35: a wire format and a release move at different rates."""
        import aer

        assert AER_ADAPTER_PROTOCOL_VERSION == "1"
        assert aer.__version__ != AER_ADAPTER_PROTOCOL_VERSION

    @pytest.mark.parametrize("event_type", sorted(ADAPTER_EVENT_TYPES, key=lambda item: item.value))
    def test_every_documented_type_is_ingestable(self, event_type: EventType) -> None:
        envelope = AgentExecutionEnvelope(event_type=event_type, identity=_identity(), payload={})
        envelope.require_ingestable()

    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            (EventType.TASK_START, "opening an adapter session"),
            (EventType.TASK_END, "closing an adapter session"),
            (EventType.VERIFICATION, "verification engine"),
        ],
    )
    def test_lifecycle_and_verdicts_have_their_own_routes(
        self, event_type: EventType, expected: str
    ) -> None:
        """The refusal names the alternative, because that is what the author needs."""
        envelope = AgentExecutionEnvelope(event_type=event_type, identity=_identity(), payload={})

        with pytest.raises(UnsupportedAdapterEvent, match=expected):
            envelope.require_ingestable()


class TestEnvelopeValidation:
    def test_a_minimal_envelope_is_valid(self) -> None:
        envelope = AgentExecutionEnvelope(
            event_type=EventType.TOOL_CALL,
            identity=_identity(),
            external_event_id="evt-1",
            action=AgentAction(kind="tool", name="shell", input_summary="pytest -q"),
        )

        assert envelope.protocol_version == AER_ADAPTER_PROTOCOL_VERSION
        assert envelope.body["name"] == "shell"

    def test_a_missing_identity_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentExecutionEnvelope(event_type=EventType.TOOL_CALL, payload={})  # type: ignore[call-arg]

    def test_a_missing_provider_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentIdentity(agent_name="x", adapter_name="y", adapter_version="1")  # type: ignore[call-arg]

    def test_an_unknown_event_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentExecutionEnvelope(
                event_type="NONSENSE",  # type: ignore[arg-type]
                identity=_identity(),
            )

    def test_extra_fields_are_refused_rather_than_ignored(self) -> None:
        """A vendor field silently absorbed is a vendor field nobody noticed."""
        with pytest.raises(ValidationError):
            AgentExecutionEnvelope(
                event_type=EventType.TOOL_CALL,
                identity=_identity(),
                vendor_specific_thing="surprise",  # type: ignore[call-arg]
            )

    def test_the_body_merges_the_payload_and_the_normalised_form(self) -> None:
        envelope = AgentExecutionEnvelope(
            event_type=EventType.TOOL_RESULT,
            identity=_identity(),
            payload={"success": False, "tool": "shell"},
            observation=AgentObservation(kind=ObservationKind.TOOL_FAILURE, summary="boom"),
        )

        assert envelope.body == {
            "success": False,
            "tool": "shell",
            "kind": "TOOL_FAILURE",
            "summary": "boom",
        }


class TestTranslationRefusals:
    def test_the_reference_adapter_rejects_an_unknown_event(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        adapter = GenericAgentAdapter()
        handle = ingestor.open(adapter, {"session_id": "s", "task": "t"})

        with pytest.raises(UnsupportedAdapterEvent, match="does not recognize"):
            ingestor.ingest(handle, {"type": "something_new"})

    def test_the_reference_adapter_ignores_a_known_but_empty_event(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """ "We chose not to record this" is a different answer from "we do not know"."""
        adapter = GenericAgentAdapter()
        handle = ingestor.open(adapter, {"session_id": "s", "task": "t"})

        result = ingestor.ingest(handle, {"type": "heartbeat"})

        assert result.outcome.value == "IGNORED"
        assert adapter_runtime.get_events(handle.run_id)[-1].event_type.value == "TASK_START"

    def test_a_session_start_without_a_task_is_rejected(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        with pytest.raises(AdapterProtocolError, match="must carry 'task'"):
            ingestor.open(GenericAgentAdapter(), {"session_id": "s"})

    def test_a_session_start_without_a_session_id_is_rejected(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        with pytest.raises(AdapterProtocolError, match="session_id"):
            ingestor.open(GenericAgentAdapter(), {"task": "t"})

    def test_an_event_without_a_type_is_rejected(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), {"session_id": "s", "task": "t"})

        with pytest.raises(AdapterProtocolError, match="must carry a 'type'"):
            ingestor.ingest(handle, {"tool": "shell"})


class TestProtocolVersionCompatibility:
    def test_a_future_protocol_is_refused_by_the_registry(self, adapter_runtime: AER) -> None:
        registry: AdapterRegistry = adapter_runtime.adapter_registry
        registry.register(FutureProtocolAdapter, name="future")

        with pytest.raises(AdapterProtocolError, match="protocol version"):
            registry.create("future")

    def test_the_check_also_fires_on_a_directly_constructed_adapter(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Not only the registry path: an instance handed straight in is checked too."""
        with pytest.raises(AdapterProtocolError, match="protocol version"):
            ingestor.open(FutureProtocolAdapter(), {"session_id": "s", "task": "t"})

    def test_an_unknown_adapter_name_lists_what_is_registered(self, adapter_runtime: AER) -> None:
        registry: AdapterRegistry = adapter_runtime.adapter_registry
        registry.register(GenericAgentAdapter)

        with pytest.raises(AdapterError, match="aer-generic"):
            registry.create("aer-typo")


class TestCapabilityDeclaration:
    def test_everything_is_off_by_default(self) -> None:
        """The default has to be "I can prove nothing about what the agent did"."""
        missing = AdapterCapabilities().missing()

        assert set(missing) == {
            "tool_events",
            "explicit_adoption_signal",
            "human_feedback",
            "decision_summary",
            "external_verification",
            "session_linkage",
            "explicit_utility_signal",
        }

    def test_the_reference_adapter_claims_only_what_it_translates(self) -> None:
        """A reference implementation that over-claimed would teach the wrong lesson."""
        capabilities = GenericAgentAdapter().capabilities()

        assert capabilities.tool_events is True
        assert capabilities.explicit_adoption_signal is False
        assert capabilities.explicit_utility_signal is False


def _identity() -> AgentIdentity:
    return AgentIdentity(
        provider="probe",
        agent_name="probe-agent",
        adapter_name="aer-probe",
        adapter_version="1",
    )
