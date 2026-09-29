"""AER -- Agent Experience Runtime (智能体经验运行时).

A lightweight, embedded runtime that turns an agent's real task executions into
traceable, verifiable and reusable experience.

Delivered so far:

* :class:`~aer.runtime.runtime.AER` -- embedded runtime entry point;
* :class:`~aer.runtime.run.RunContext` -- per-task lifecycle handle;
* :class:`~aer.runtime.hooks.ToolContext` / ``RecoveryContext`` -- hooks that make
  tool calls and repair attempts observable even when they raise;
* core domain models -- ``Run``, ``Event``, ``ErrorRecord``, ``RecoveryRecord``,
  ``VerificationRecord``, ``Experience``;
* independent verification -- :class:`~aer.verification.engine.VerificationEngine`
  plus deterministic/environment/human/LLM verifiers, so "the agent said it was
  done" and "it is done" stay two separate, durable facts;
* experience distillation -- :class:`~aer.experience.service.ExperienceService`
  turns a finished run into a ``SUCCESS``, ``RECOVERY`` or ``FAILURE`` experience
  with recorded provenance and a lifecycle-only status;
* SQLite persistence behind a repository layer, with the schema managed by Alembic;
* knowledge retrieval -- :mod:`aer.knowledge` projects the experience store into a
  searchable graph index and ranks what comes back, with NeuG as a rebuildable copy
  rather than a second source of truth;
* experience usage -- :mod:`aer.usage` records what actually happened to a retrieved
  experience, so the four distinctions the runtime is built on stay measurable::

      Retrieved != Injected != Adopted != Helpful
      Task success != Experience caused success

  plus the effectiveness report, the deterministic confidence and the
  ``VERIFIED -> REUSED -> PROVEN`` promotion policy;
* agent adapters -- :mod:`aer.adapter` is the protocol through which Codex, Claude Code,
  Cursor, DSH and anything else report into AER. The core never learns a vendor's format:
  an adapter translates vendor payloads into protocol envelopes, and AER decides what
  they mean. External input is untrusted, so it is redacted and size-capped on the way
  in; an adapter that cannot observe something is refused when it tries to report it;
* a deployment config (:func:`~aer.config.load_deployment_config`) that resolves
  data/artifact/knowledge locations from the environment, so the same build runs
  from a checkout, a CI runner and a container without branching.

Quick start::

    from aer import AER, EventType, HttpStatusVerifier, VerificationContext

    with AER("./data", distillation_provider=my_provider) as aer:
        run = aer.start_run(task="Fix WordPress product page H1", task_type="wordpress")
        run.emit(EventType.MODEL_CALL, input={"prompt": "rewrite the H1"})

        with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
            tool.set_result(update_page())

        run.success()

        # Independent of whatever the agent claimed:
        run.verify(
            HttpStatusVerifier(expected_status=200),
            context=VerificationContext(
                run_id=run.run_id, payload={"actual_status": 200},
            ),
        )
        print(run.verified_success())

        # And only then worth learning from:
        experience = run.distill()

A later task retrieves that knowledge, and records what became of it::

    tracked = aer.retrieve_for_run(run_id=later_run.id, query="WordPress REST API 403")
    aer.record_injection(
        session_id=tracked.session_id,
        experience_ids=[hit.experience_id for hit in tracked.result.guidance],
        context_fingerprint=...,
        formatter_version=FORMATTER_VERSION,
    )
    aer.record_usage_signal(
        session_id=tracked.session_id,
        experience_id=experience.id,
        signal=UsageSignal.ADOPTED,
        source=UsageSignalSource.AGENT,
    )
    print(aer.experience_effectiveness(experience.id).describe())

Import order below is alphabetical and carries no hidden meaning: the layers are
independent by construction. The facade is reachable only as ``aer.AER`` /
``aer.runtime.runtime.AER``, never re-exported from ``aer.runtime``, because it is a
composition root that imports the application layers (see
:mod:`aer.runtime`).
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
    sanitize_external_body,
)
from aer.config import DeploymentConfig, load_deployment_config
from aer.exceptions import (
    AdapterCapabilityError,
    AdapterError,
    AdapterProtocolError,
    AdapterSessionTerminated,
    AERError,
    CandidateValidationError,
    ConfigurationError,
    DistillationError,
    ExperienceError,
    ExperienceLifecycleError,
    HookStateError,
    KnowledgeError,
    KnowledgeIndexUnavailable,
    KnowledgeQueryError,
    KnowledgeSchemaError,
    ProjectionError,
    RecordNotFoundError,
    RunStateError,
    StorageError,
    UnsupportedAdapterEvent,
    UsageError,
    UsageTrackingError,
    VerificationError,
    VerificationInputError,
)
from aer.experience.candidate import (
    ExperienceCandidate,
    normalise_candidate,
    validate_candidate,
)
from aer.experience.classify import classify_kind, outcome_is_verified
from aer.experience.dedup import dedup_key_for, normalise_text
from aer.experience.distiller import ExperienceDistiller
from aer.experience.evidence import RunEvidence, RunEvidenceBuilder
from aer.experience.policy import DEFAULT_POLICY, DistillationDecision, DistillationPolicy
from aer.experience.provider import CallableDistillationProvider, DistillationProvider
from aer.experience.service import ExperienceService
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.knowledge.models import (
    DEFAULT_RETRIEVAL_LIMIT,
    MAX_RETRIEVAL_LIMIT,
    ExperienceSearchQuery,
    RetrievalHit,
    RetrievalResult,
)
from aer.knowledge.projector import (
    DriftReport,
    KnowledgeProjector,
    ProjectionOutcome,
    RebuildReport,
)
from aer.knowledge.retriever import RETRIEVAL_POLICY_VERSION
from aer.knowledge.status import KnowledgeStatus
from aer.runtime.enums import (
    AdapterIngestOutcome,
    AdapterSessionOutcome,
    DistillationTrigger,
    EventType,
    ExperienceKind,
    ExperienceStatus,
    ProjectionAction,
    RetrievalMode,
    RunOutcome,
    RunStatus,
    SessionAssignment,
    UsageRole,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
    VerifierType,
)
from aer.runtime.external import ExternalEventRecorder
from aer.runtime.hooks import RecoveryContext, ToolContext
from aer.runtime.lifecycle import (
    ALLOWED_TRANSITIONS,
    allowed_transitions_from,
    can_transition,
    is_at_least,
    is_terminal,
    reachable_from,
    status_rank,
)
from aer.runtime.models import (
    AdapterEventRecord,
    AdapterSession,
    ErrorRecord,
    Event,
    Experience,
    ExperienceSource,
    ExperienceUsage,
    RecoveryRecord,
    RetrievalSession,
    Run,
    VerificationRecord,
)
from aer.runtime.run import RunContext
from aer.runtime.runtime import AER
from aer.usage.confidence import (
    DEFAULT_CONFIDENCE_WEIGHTS,
    DEFAULT_REUSE_SCALE,
    NO_FEEDBACK_PRIOR,
    ConfidenceWeights,
    ExperienceConfidence,
    ExperienceConfidenceService,
)
from aer.usage.effectiveness import (
    ExperienceEffectivenessReport,
    ExperienceEffectivenessService,
    classify_outcome,
)
from aer.usage.fingerprints import (
    RETRIEVAL_QUERY_MAX_LENGTH,
    context_fingerprint,
    query_fingerprint,
    sanitize_query,
)
from aer.usage.promotion import (
    DEFAULT_PROMOTION_POLICY,
    ExperiencePromotionService,
    PromotionDecision,
    PromotionPolicy,
)
from aer.usage.tracking import ExperienceUsageService, TrackedRetrievalResult
from aer.verification.base import (
    CallableVerifier,
    VerificationContext,
    VerificationResult,
    Verifier,
    VerifierBase,
)
from aer.verification.deterministic import (
    DeterministicVerifier,
    H1CountVerifier,
    HttpStatusVerifier,
    JsonValidVerifier,
    PredicateVerifier,
)
from aer.verification.engine import VerificationEngine
from aer.verification.environment import CallableEnvironmentVerifier
from aer.verification.human import HumanVerifier
from aer.verification.llm import LLMVerifier
from aer.verification.summary import VerificationSummary, is_verified_success

__version__ = "0.8.1"

__all__ = [
    "ADAPTER_EVENT_TYPES",
    "AER",
    "AER_ADAPTER_PROTOCOL_VERSION",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_CONFIDENCE_WEIGHTS",
    "DEFAULT_POLICY",
    "DEFAULT_PROMOTION_POLICY",
    "DEFAULT_RETRIEVAL_LIMIT",
    "DEFAULT_REUSE_SCALE",
    "FORMATTER_VERSION",
    "MAX_BODY_CHARS",
    "MAX_DECISION_SUMMARY_CHARS",
    "MAX_RETRIEVAL_LIMIT",
    "MAX_STRING_CHARS",
    "NO_FEEDBACK_PRIOR",
    "PRIVATE_REASONING_KEYS",
    "RETRIEVAL_POLICY_VERSION",
    "RETRIEVAL_QUERY_MAX_LENGTH",
    "AERError",
    "AdapterCapabilities",
    "AdapterCapabilityError",
    "AdapterError",
    "AdapterEventRecord",
    "AdapterFinishRequest",
    "AdapterIngestOutcome",
    "AdapterIngestor",
    "AdapterProtocolError",
    "AdapterRegistry",
    "AdapterSession",
    "AdapterSessionHandle",
    "AdapterSessionOutcome",
    "AdapterSessionRequest",
    "AdapterSessionTerminated",
    "AgentAction",
    "AgentAdapter",
    "AgentExecutionEnvelope",
    "AgentIdentity",
    "AgentObservation",
    "CallableDistillationProvider",
    "CallableEnvironmentVerifier",
    "CallableVerifier",
    "CandidateValidationError",
    "ConfidenceWeights",
    "ConfigurationError",
    "DeploymentConfig",
    "DeterministicVerifier",
    "DistillationDecision",
    "DistillationError",
    "DistillationPolicy",
    "DistillationProvider",
    "DistillationTrigger",
    "DriftReport",
    "ErrorRecord",
    "Event",
    "EventType",
    "Experience",
    "ExperienceCandidate",
    "ExperienceConfidence",
    "ExperienceConfidenceService",
    "ExperienceContextFormatter",
    "ExperienceDistiller",
    "ExperienceEffectivenessReport",
    "ExperienceEffectivenessService",
    "ExperienceError",
    "ExperienceKind",
    "ExperienceLifecycleError",
    "ExperiencePromotionService",
    "ExperienceSearchQuery",
    "ExperienceService",
    "ExperienceSource",
    "ExperienceStatus",
    "ExperienceUsage",
    "ExperienceUsageService",
    "ExternalEventRecorder",
    "GenericAgentAdapter",
    "H1CountVerifier",
    "HookStateError",
    "HttpStatusVerifier",
    "HumanVerifier",
    "IngestResult",
    "JsonValidVerifier",
    "KnowledgeError",
    "KnowledgeIndexUnavailable",
    "KnowledgeProjector",
    "KnowledgeQueryError",
    "KnowledgeSchemaError",
    "KnowledgeStatus",
    "LLMVerifier",
    "ObservationKind",
    "PredicateVerifier",
    "ProjectionAction",
    "ProjectionError",
    "ProjectionOutcome",
    "PromotionDecision",
    "PromotionPolicy",
    "RebuildReport",
    "RecordNotFoundError",
    "RecoveryContext",
    "RecoveryRecord",
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalResult",
    "RetrievalSession",
    "Run",
    "RunContext",
    "RunEvidence",
    "RunEvidenceBuilder",
    "RunOutcome",
    "RunStateError",
    "RunStatus",
    "SessionAssignment",
    "StorageError",
    "ToolContext",
    "TrackedRetrievalResult",
    "UnsupportedAdapterEvent",
    "UsageError",
    "UsageRole",
    "UsageSignal",
    "UsageSignalSource",
    "UsageTrackingError",
    "UtilityLabel",
    "UtilitySource",
    "VerificationContext",
    "VerificationEngine",
    "VerificationError",
    "VerificationInputError",
    "VerificationRecord",
    "VerificationResult",
    "VerificationSummary",
    "Verifier",
    "VerifierBase",
    "VerifierType",
    "__version__",
    "allowed_transitions_from",
    "can_transition",
    "classify_kind",
    "classify_outcome",
    "context_fingerprint",
    "dedup_key_for",
    "is_at_least",
    "is_terminal",
    "is_verified_success",
    "load_deployment_config",
    "normalise_candidate",
    "normalise_text",
    "outcome_is_verified",
    "query_fingerprint",
    "reachable_from",
    "sanitize_external_body",
    "sanitize_query",
    "status_rank",
    "validate_candidate",
]
