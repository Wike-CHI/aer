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
* a deployment config (:func:`~aer.config.load_deployment_config`) that resolves
  data/artifact/knowledge locations from the environment, so the same build runs
  from a checkout, a CI runner and a container without branching.

Retrieval and experience usage are later milestones.

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

Import order below is alphabetical and carries no hidden meaning: the layers are
independent by construction. The facade is reachable only as ``aer.AER`` /
``aer.runtime.runtime.AER``, never re-exported from ``aer.runtime``, because it is a
composition root that imports the application layers (see
:mod:`aer.runtime`).
"""

from aer.config import DeploymentConfig, load_deployment_config
from aer.exceptions import (
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
from aer.knowledge.formatter import ExperienceContextFormatter
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
from aer.knowledge.status import KnowledgeStatus
from aer.runtime.enums import (
    DistillationTrigger,
    EventType,
    ExperienceKind,
    ExperienceStatus,
    ProjectionAction,
    RetrievalMode,
    RunStatus,
    VerifierType,
)
from aer.runtime.hooks import RecoveryContext, ToolContext
from aer.runtime.lifecycle import (
    ALLOWED_TRANSITIONS,
    allowed_transitions_from,
    can_transition,
    is_terminal,
    reachable_from,
)
from aer.runtime.models import (
    ErrorRecord,
    Event,
    Experience,
    ExperienceSource,
    RecoveryRecord,
    Run,
    VerificationRecord,
)
from aer.runtime.run import RunContext
from aer.runtime.runtime import AER
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

__version__ = "0.6.0"

__all__ = [
    "AER",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_POLICY",
    "DEFAULT_RETRIEVAL_LIMIT",
    "MAX_RETRIEVAL_LIMIT",
    "AERError",
    "CallableDistillationProvider",
    "CallableEnvironmentVerifier",
    "CallableVerifier",
    "CandidateValidationError",
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
    "ExperienceContextFormatter",
    "ExperienceDistiller",
    "ExperienceError",
    "ExperienceKind",
    "ExperienceLifecycleError",
    "ExperienceSearchQuery",
    "ExperienceService",
    "ExperienceSource",
    "ExperienceStatus",
    "H1CountVerifier",
    "HookStateError",
    "HttpStatusVerifier",
    "HumanVerifier",
    "JsonValidVerifier",
    "KnowledgeError",
    "KnowledgeIndexUnavailable",
    "KnowledgeProjector",
    "KnowledgeQueryError",
    "KnowledgeSchemaError",
    "KnowledgeStatus",
    "LLMVerifier",
    "PredicateVerifier",
    "ProjectionAction",
    "ProjectionError",
    "ProjectionOutcome",
    "RebuildReport",
    "RecordNotFoundError",
    "RecoveryContext",
    "RecoveryRecord",
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalResult",
    "Run",
    "RunContext",
    "RunEvidence",
    "RunEvidenceBuilder",
    "RunStateError",
    "RunStatus",
    "StorageError",
    "ToolContext",
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
    "dedup_key_for",
    "is_terminal",
    "is_verified_success",
    "load_deployment_config",
    "normalise_candidate",
    "normalise_text",
    "outcome_is_verified",
    "reachable_from",
    "validate_candidate",
]
