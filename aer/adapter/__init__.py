"""Agent adapters: one protocol, many Agents, no vendor knowledge in the core.

    Codex / Claude Code / Cursor / DSH / in-house agents
                        │
                        ▼   vendor-specific translation (a later milestone, M8.x)
                 Agent-specific Adapter
                        │
                        ▼   this package
                 Agent Adapter Protocol
                        │
                        ▼
                    AER Runtime

The problem this package solves is a scale problem rather than a coding one: AER's value
comes from the *volume* of real executions it can learn from, and that volume lives
inside other people's agents. Without a protocol, every integration would be a bespoke
script reaching into AER's internals, and the second one would quietly disagree with the
first about what an event means. With one, an adapter is a translation table and nothing
more.

Four properties make that work, and each is a module here:

* **the vocabulary is AER's** (:mod:`~aer.adapter.protocol`). An adapter maps *into* AER's
  events; AER never grows an event type because a vendor has one (sections 5, 53). The
  protocol version is named separately from the package version, because a wire format
  and a release move at completely different rates (section 35);
* **an adapter cannot touch storage** (:mod:`~aer.adapter.ingest`). It has no
  repository, no engine, and no way to end a run except by saying the session ended. The
  terminal-state guard, the single error pipeline and the sequence allocation all stay
  on AER's side of the line (section 10);
* **external input is untrusted** (:mod:`~aer.adapter.sanitize`). Credentials are
  redacted by value and by key name, private reasoning is dropped, and sizes are capped
  with an explicit marker (sections 25-27);
* **capabilities are declarations, and they are enforced**. An adapter that never
  claimed it can see whether the agent used an experience is refused when it tries to say
  that it did (sections 11-12) -- because a guessed ``ADOPTED`` is indistinguishable from
  an observed one once stored, and it is the stronger claim.

Deliberately absent: any specific vendor adapter, and any consumer of the usage data.
``Codex``, ``Claude Code``, ``Cursor`` and ``DSH`` are M8.1-M8.4; dataset generation is a
later milestone entirely, and the order that matters is not by brand but by which platform
can supply the most complete hooks (sections 57 and 58).

The reference implementation is :class:`~aer.adapter.generic.GenericAgentAdapter`: a small
documented JSON vocabulary that drives every AER capability. It exists to prove the
protocol is sufficient, and to let the test suite show two unrelated agent vocabularies
producing identical AER evidence (sections 30-31, 60).
"""

from aer.adapter.generic import GenericAgentAdapter
from aer.adapter.ingest import AdapterIngestor, AdapterSessionHandle, IngestResult
from aer.adapter.protocol import (
    ADAPTER_EVENT_TYPES,
    AER_ADAPTER_PROTOCOL_VERSION,
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterSessionRequest,
    AgentAction,
    AgentAdapter,
    AgentExecutionEnvelope,
    AgentIdentity,
    AgentObservation,
    ObservationKind,
)
from aer.adapter.registry import AdapterRegistry
from aer.adapter.sanitize import (
    MAX_BODY_CHARS,
    MAX_DECISION_SUMMARY_CHARS,
    MAX_STRING_CHARS,
    PRIVATE_REASONING_KEYS,
    SanitizedBody,
    sanitize_external_body,
    sanitize_external_value,
)

__all__ = [
    "ADAPTER_EVENT_TYPES",
    "AER_ADAPTER_PROTOCOL_VERSION",
    "MAX_BODY_CHARS",
    "MAX_DECISION_SUMMARY_CHARS",
    "MAX_STRING_CHARS",
    "PRIVATE_REASONING_KEYS",
    "AdapterCapabilities",
    "AdapterFinishRequest",
    "AdapterIngestor",
    "AdapterRegistry",
    "AdapterSessionHandle",
    "AdapterSessionRequest",
    "AgentAction",
    "AgentAdapter",
    "AgentExecutionEnvelope",
    "AgentIdentity",
    "AgentObservation",
    "GenericAgentAdapter",
    "IngestResult",
    "ObservationKind",
    "SanitizedBody",
    "sanitize_external_body",
    "sanitize_external_value",
]
