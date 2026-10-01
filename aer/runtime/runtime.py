"""The embedded AER runtime facade (Milestone 1 Task 3.1/Task 3.2 shape).

.. code-block:: python

    from aer import AER, EventType

    aer = AER("./data")
    run = aer.start_run(task="Fix WordPress H1", task_type="wordpress")

Usage is entirely in-process. There is no server, no Redis, no external database
to start: constructing ``AER`` creates the data directory, brings the SQLite schema
up to the latest Alembic revision and opens the engine. This is the "Embedded Mode"
requirement of the milestone.

``AER`` owns the object graph -- one :class:`~aer.storage.database.Database` and
the repositories built on it -- and hands out :class:`~aer.runtime.run.RunContext`
objects. It contains no SQL and no ORM access of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from aer.adapter.ingest import AdapterIngestor, AdapterSessionHandle, IngestResult
from aer.adapter.protocol import AER_ADAPTER_PROTOCOL_VERSION, AgentAdapter
from aer.adapter.registry import AdapterRegistry
from aer.exceptions import AERError, KnowledgeError, RecordNotFoundError
from aer.experience.evidence import RunEvidence, RunEvidenceBuilder
from aer.experience.policy import DistillationDecision, DistillationPolicy
from aer.experience.provider import DistillationProvider
from aer.experience.service import ExperienceService
from aer.knowledge.base import KnowledgeIndex
from aer.knowledge.formatter import ExperienceContextFormatter
from aer.knowledge.models import (
    DEFAULT_RETRIEVAL_LIMIT,
    ExperienceSearchQuery,
    RetrievalResult,
)
from aer.knowledge.projector import KnowledgeProjector, ProjectionOutcome, RebuildReport
from aer.knowledge.retriever import ExperienceRetriever
from aer.knowledge.status import KnowledgeStatus, collect_status
from aer.runtime.enums import (
    EventType,
    ExperienceKind,
    ExperienceStatus,
    RetrievalMode,
    RunStatus,
    SessionAssignment,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)
from aer.runtime.models import (
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
from aer.runtime.serialization import JsonObject, new_id, to_json_object
from aer.storage.database import Database
from aer.storage.repositories import (
    AdapterEventRepository,
    AdapterSessionRepository,
    ErrorRepository,
    EventRepository,
    ExperienceRepository,
    ExperienceSourceRepository,
    ExperienceUsageRepository,
    RecoveryRepository,
    RetrievalSessionRepository,
    RunRepository,
    VerificationRepository,
)
from aer.usage.confidence import ExperienceConfidence, ExperienceConfidenceService
from aer.usage.effectiveness import (
    ExperienceEffectivenessReport,
    ExperienceEffectivenessService,
)
from aer.usage.promotion import (
    ExperiencePromotionService,
    PromotionDecision,
    PromotionPolicy,
)
from aer.usage.tracking import ExperienceUsageService, TrackedRetrievalResult
from aer.verification.base import VerificationContext, Verifier
from aer.verification.engine import VerificationEngine
from aer.verification.summary import VerificationSummary

#: Default data directory, matching the agreed layout (``./data/aer.db``).
DEFAULT_DATA_DIR = "./data"
#: SQLite file name inside the data directory.
DEFAULT_DB_FILENAME = "aer.db"
#: Default knowledge directory, a sibling of ``data`` exactly as ``/knowledge`` is a
#: sibling of ``/data`` in the deployment.
DEFAULT_KNOWLEDGE_DIR = "knowledge"
#: Directory name of the embedded graph index inside the knowledge directory.
KNOWLEDGE_DATABASE_NAME = "aer-knowledge"

logger = logging.getLogger(__name__)


class AER:
    """Embedded Agent Experience Runtime."""

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        db_filename: str = DEFAULT_DB_FILENAME,
        echo: bool = False,
        distillation_provider: DistillationProvider | None = None,
        distillation_policy: DistillationPolicy | None = None,
        knowledge_dir: str | Path | None = None,
        knowledge_index: KnowledgeIndex | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._knowledge_dir = Path(
            DEFAULT_KNOWLEDGE_DIR if knowledge_dir is None else knowledge_dir
        )
        # Nothing above touches the knowledge plane: the index is built on first
        # use, so constructing an AER -- which every agent does -- never imports the
        # graph engine, never opens a second database and never fails because the
        # index is missing. Section 79 in one line.
        self._knowledge_index = knowledge_index
        self._knowledge_injected = knowledge_index is not None
        self._projector: KnowledgeProjector | None = None
        self._retriever: ExperienceRetriever | None = None

        self._database = Database(self._data_dir / db_filename, echo=echo)
        self._runs = RunRepository(self._database)
        self._events = EventRepository(self._database)
        self._errors = ErrorRepository(self._database)
        self._recoveries = RecoveryRepository(self._database)
        self._verifications = VerificationRepository(self._database)
        self._experiences = ExperienceRepository(self._database)
        self._experience_sources = ExperienceSourceRepository(self._database)
        self._retrieval_sessions = RetrievalSessionRepository(self._database)
        self._experience_usage = ExperienceUsageRepository(self._database)
        self._adapter_sessions = AdapterSessionRepository(self._database)
        self._adapter_events = AdapterEventRepository(self._database)
        # The Agent-adapter surface is SQLite-only apart from the retrieval call an
        # adapter may make, so the registry and the ingestor can be wired eagerly. An
        # application that never registers an adapter pays for two repositories.
        self._adapter_registry = AdapterRegistry()
        self._adapter_ingestor = AdapterIngestor(
            runtime=self,
            sessions=self._adapter_sessions,
            events=self._adapter_events,
            registry=self._adapter_registry,
        )
        self._verification_engine = VerificationEngine(
            runs=self._runs,
            verifications=self._verifications,
        )
        # Milestone 7 analytics are SQLite-only, so they can be wired eagerly: none
        # of them touches the knowledge plane, and constructing an AER must not make
        # the graph engine's absence into a startup failure (round-6 brief, section 79).
        self._effectiveness = ExperienceEffectivenessService(
            experiences=self._experiences,
            usage=self._experience_usage,
            sessions=self._retrieval_sessions,
            runs=self._runs,
            verifications=self._verifications,
        )
        self._promotion = ExperiencePromotionService(
            experiences=self._experiences,
            sources=self._experience_sources,
            usage=self._experience_usage,
            effectiveness=self._effectiveness,
        )
        self._confidence = ExperienceConfidenceService(
            experiences=self._experiences,
            effectiveness=self._effectiveness,
        )
        # Usage *tracking* needs the retriever (and therefore the index), so it is
        # built on first use like the retriever itself.
        self._usage_service: ExperienceUsageService | None = None
        # Distillation is optional at construction: AER must stay usable without a
        # model configured, and a missing provider is reported loudly on first use
        # rather than silently skipping every distillation.
        self._experience_service = ExperienceService(
            runtime=self,
            experiences=self._experiences,
            sources=self._experience_sources,
            evidence=RunEvidenceBuilder(
                runs=self._runs,
                events=self._events,
                errors=self._errors,
                recoveries=self._recoveries,
                verifications=self._verifications,
            ),
            policy=distillation_policy,
            provider=distillation_provider,
        )
        self._closed = False

    # -- introspection -----------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """Directory holding the database (and, later, artifacts/datasets)."""
        return self._data_dir

    @property
    def database(self) -> Database:
        """The storage kernel. Reserved for diagnostics and tests."""
        return self._database

    @property
    def runs(self) -> RunRepository:
        """Run repository. Business agents normally go through ``RunContext``."""
        return self._runs

    @property
    def events(self) -> EventRepository:
        """Event repository. Business agents normally go through ``RunContext``."""
        return self._events

    @property
    def errors(self) -> ErrorRepository:
        """Error repository.

        Named ``errors`` rather than ``error_repository`` because this is the
        handle callers reach for constantly; the exception module was renamed to
        :mod:`aer.exceptions` to keep the two unambiguous (docs/DECISIONS.md D-011).
        """
        return self._errors

    @property
    def recoveries(self) -> RecoveryRepository:
        """Recovery repository."""
        return self._recoveries

    @property
    def verifications(self) -> VerificationRepository:
        """Verification repository. Business agents normally go through
        ``RunContext.verify`` so that the event and the record are written together.
        """
        return self._verifications

    @property
    def verification_engine(self) -> VerificationEngine:
        """The one verification pipeline.

        Named explicitly (rather than ``verification``) so it cannot be confused
        with the ``verifications`` repository above: the engine *runs* verifiers and
        writes both the event and the record, the repository only reads rows.
        """
        return self._verification_engine

    @property
    def experiences(self) -> ExperienceRepository:
        """Experience repository. Reading knowledge is a normal thing to do directly."""
        return self._experiences

    @property
    def experience_sources(self) -> ExperienceSourceRepository:
        """Provenance links between experiences and runs."""
        return self._experience_sources

    @property
    def experience_service(self) -> ExperienceService:
        """The one distillation pipeline (brief section 26)."""
        return self._experience_service

    # -- usage (Milestone 7) ----------------------------------------------

    @property
    def retrieval_sessions(self) -> RetrievalSessionRepository:
        """Recorded retrieval sessions. Reading them is a normal thing to do."""
        return self._retrieval_sessions

    @property
    def experience_usage(self) -> ExperienceUsageRepository:
        """Recorded usage rows: one per experience per retrieval session."""
        return self._experience_usage

    @property
    def usage_tracking(self) -> ExperienceUsageService:
        """The writer for every usage fact.

        Built on first use because it owns the retriever, and through it the
        knowledge index. Reaching for this property is therefore the moment a runtime
        may need the graph engine -- which is exactly why constructing ``AER`` does
        not do it.
        """
        self._ensure_open()
        if self._usage_service is None:
            self._usage_service = ExperienceUsageService(
                retriever=self.retriever,
                sessions=self._retrieval_sessions,
                usage=self._experience_usage,
                runs=self._runs,
            )
        return self._usage_service

    @property
    def effectiveness(self) -> ExperienceEffectivenessService:
        """The service that joins usage to runs and verdicts."""
        return self._effectiveness

    @property
    def promotion(self) -> ExperiencePromotionService:
        """The ``VERIFIED -> REUSED -> PROVEN`` policy and its application."""
        return self._promotion

    @property
    def confidence(self) -> ExperienceConfidenceService:
        """On-demand, deterministic confidence over one experience's evidence."""
        return self._confidence

    # -- agent adapters (Milestone 8) --------------------------------------

    @property
    def adapter_registry(self) -> AdapterRegistry:
        """Registered Agent adapters, by name.

        The extension point for integrations: register a factory once, then open
        sessions by name. Deliberately a plain mapping rather than a plugin system --
        an adapter is a translation table, not an application (round-8 brief, section 37).
        """
        return self._adapter_registry

    @property
    def adapter_ingestor(self) -> AdapterIngestor:
        """The AER side of the adapter protocol.

        Everything an external Agent is allowed to do to this runtime goes through it:
        opening and closing sessions, delivering translated events, and recording the
        usage facts an integration can actually observe.
        """
        return self._adapter_ingestor

    @property
    def adapter_sessions(self) -> AdapterSessionRepository:
        """Persisted ``external session -> AER run`` mappings."""
        return self._adapter_sessions

    @property
    def adapter_events(self) -> AdapterEventRepository:
        """The idempotency ledger for external events."""
        return self._adapter_events

    def open_adapter_session(
        self,
        adapter: AgentAdapter | str,
        raw: Mapping[str, object],
        *,
        task_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AdapterSessionHandle:
        """Open or resume the run for an external Agent session.

        The convenience wrapper an integration's hook calls on connect. ``adapter`` is
        an adapter instance or the name of a registered one.

        Raises:
            AdapterError: no adapter is registered under that name.
            AdapterProtocolError: a protocol version mismatch, or an unusable translation.
            AdapterSessionTerminated: the session's run already finished; use
                ``aer.adapter_ingestor.reopen(...)`` to start a new episode (section 43).
        """
        self._ensure_open()
        return self._adapter_ingestor.open(adapter, raw, task_type=task_type, metadata=metadata)

    def ingest_adapter_event(
        self, handle: AdapterSessionHandle, raw: Mapping[str, object]
    ) -> IngestResult:
        """Translate and apply one external event to ``handle``'s run.

        Raises:
            AdapterError: the adapter crashed. The run is left untouched, because an
                integration bug must not look like an agent failure (section 28).
            UnsupportedAdapterEvent: the event type is outside the protocol vocabulary.
            RunStateError: the run already finished and the event is not an observation.
        """
        self._ensure_open()
        return self._adapter_ingestor.ingest(handle, raw)

    def close_adapter_session(
        self, handle: AdapterSessionHandle, raw: Mapping[str, object]
    ) -> Run | None:
        """Translate a session-end payload and finish the run when it says how.

        Returns ``None`` when the platform did not report a terminal status: the run
        stays ``RUNNING`` rather than having a verdict invented for it.
        """
        self._ensure_open()
        return self._adapter_ingestor.close(handle, raw)

    def find_adapter_session(
        self, provider: str, external_session_id: str
    ) -> AdapterSession | None:
        """The persisted mapping for an external session, or ``None``.

        The reconnect lookup: an integration that restarted can ask this before deciding
        whether to open anything.
        """
        self._ensure_open()
        return self._adapter_sessions.find(provider, external_session_id)

    def adapter_status(self) -> JsonObject:
        """Registered adapters plus the integration's persisted state, in one object.

        The debugging surface section 54 asks for, and nothing more: no dashboard, no
        server. ``unapplied_events`` is the number worth watching -- it counts external
        events that were claimed and never finished being applied, which is the visible
        trace of an ingest that died in the middle.
        """
        self._ensure_open()
        return to_json_object(
            {
                "protocol_version": AER_ADAPTER_PROTOCOL_VERSION,
                "adapters": [dict(item) for item in self._adapter_registry.describe()],
                "sessions": self._adapter_sessions.count(),
                "events": self._adapter_events.count(),
                "unapplied_events": self._adapter_events.count(applied=False),
            }
        )

    # -- knowledge plane ---------------------------------------------------

    @property
    def knowledge_dir(self) -> Path:
        """Directory holding the knowledge index. Created on first use."""
        return self._knowledge_dir

    @property
    def knowledge_index(self) -> KnowledgeIndex:
        """The searchable projection of the experience store.

        Built here, not in ``__init__``, so that opening a runtime stays free of the
        graph engine. An injected index (tests, an alternative implementation) is
        returned as-is.

        Raises:
            KnowledgeIndexUnavailable: the engine is not importable, or the index
                cannot be opened.
        """
        self._ensure_open()
        if self._knowledge_index is None:
            from aer.knowledge.neug import NeuGKnowledgeIndex

            self._knowledge_index = NeuGKnowledgeIndex(
                self._knowledge_dir / KNOWLEDGE_DATABASE_NAME
            )
        return self._knowledge_index

    @property
    def knowledge_projector(self) -> KnowledgeProjector:
        """The one-way copier from SQLite into the knowledge index."""
        self._ensure_open()
        if self._projector is None:
            self._projector = KnowledgeProjector(
                self.knowledge_index,
                experiences=self._experiences,
                sources=self._experience_sources,
                runs=self._runs,
            )
        return self._projector

    @property
    def retriever(self) -> ExperienceRetriever:
        """Policy-driven retrieval over the knowledge index."""
        self._ensure_open()
        if self._retriever is None:
            self._retriever = ExperienceRetriever(self.knowledge_index)
        return self._retriever

    def retrieve(
        self,
        query: str,
        *,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = DEFAULT_RETRIEVAL_LIMIT,
        include_deprecated: bool = False,
    ) -> RetrievalResult:
        """Find past experience relevant to ``query``.

        Defaults to :attr:`~aer.runtime.enums.RetrievalMode.GUIDANCE` and three
        results, because the common caller is an agent about to act and the useful
        answer is short and trustworthy.

        Raises:
            KnowledgeIndexUnavailable: the index cannot be reached. Deliberately not
                an empty result: an agent that cannot tell "nothing is known" from
                "the knowledge base is down" will confidently proceed as if it had
                checked.
            KnowledgeQueryError: ``query`` has no searchable text.
        """
        return self.retriever.retrieve(
            ExperienceSearchQuery(
                query=query,
                domain=domain,
                mode=mode,
                limit=limit,
                include_deprecated=include_deprecated,
            )
        )

    def experience_context(
        self,
        query: str,
        *,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = DEFAULT_RETRIEVAL_LIMIT,
        max_chars: int | None = None,
    ) -> str:
        """Retrieve and render, ready to place in an agent's context.

        Rendering is a separate step from retrieval on purpose: the formatter is
        where the trust labels and the "this is data, not instructions" framing live,
        and a caller that wants the structured result should use :meth:`retrieve`
        rather than parse this string back.
        """
        result = self.retrieve(query, domain=domain, mode=mode, limit=limit)
        formatter = (
            ExperienceContextFormatter()
            if max_chars is None
            else ExperienceContextFormatter(max_chars=max_chars)
        )
        return formatter.format(result)

    def project_experience(self, experience_id: str) -> ProjectionOutcome:
        """Copy one experience (or its deletion) into the knowledge index.

        The projection follows a committed store write; it never precedes one and
        never participates in one. A failure here leaves the experience intact and
        the projection stale, which :meth:`knowledge_status` will report and
        :meth:`rebuild_knowledge` will fix.
        """
        return self.knowledge_projector.project_experience(experience_id)

    def project_run(self, run_id: str) -> tuple[ProjectionOutcome, ...]:
        """Project every experience distilled from ``run_id``."""
        return self.knowledge_projector.project_run(run_id)

    def project_experiences(self) -> int:
        """Catch the index up with the store. Returns how many were written."""
        return self.knowledge_projector.project_all()

    def rebuild_knowledge(self) -> RebuildReport:
        """Rebuild the index from SQLite and swap it in.

        The repair for every knowledge-plane problem, because the index is a
        projection: a corrupt, stale or wrong-shaped index is not restored, it is
        recomputed.
        """
        return self.knowledge_projector.rebuild()

    def knowledge_status(self) -> KnowledgeStatus:
        """Reachability, schema version, counts and drift, in one report."""
        self._ensure_open()
        return collect_status(
            self.knowledge_index,
            self.knowledge_projector,
            store_experiences=self._experiences.count(include_deprecated=True),
        )

    @property
    def is_closed(self) -> bool:
        """``True`` after :meth:`close` has been called."""
        return self._closed

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"AER(data_dir={self._data_dir.as_posix()!r}, {state})"

    # -- run lifecycle -----------------------------------------------------

    def start_run(
        self,
        task: str,
        *,
        task_type: str | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        """Create a run, persist it, record ``TASK_START`` and return its context.

        Args:
            task: What the agent was asked to do (stored as ``task_description``).
            task_type: Free-form task category, e.g. ``"wordpress"``.
            agent_name / agent_version: Identity of the executing agent.
            model_provider / model_name: Model backing the agent, when applicable.
            metadata: Extra JSON metadata attached to the run.
            run_id: Explicit identifier; a UUID4 is generated when omitted.

        Raises:
            AERError: the runtime has been closed.
            StorageError: the run or its ``TASK_START`` event could not be written.
        """
        self._ensure_open()

        run = Run(
            id=run_id if run_id is not None else new_id(),
            task_description=task,
            task_type=task_type,
            agent_name=agent_name,
            agent_version=agent_version,
            model_provider=model_provider,
            model_name=model_name,
            metadata=to_json_object(metadata),
        )
        self._runs.create(run)

        context = RunContext(self, run)
        context.emit(
            EventType.TASK_START,
            input={"task": task, "task_type": task_type},
        )
        return context

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> Run | None:
        """Load a run by id, or ``None`` when it does not exist."""
        self._ensure_open()
        return self._runs.get(run_id)

    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        task_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[Run]:
        """List persisted runs, newest first by default."""
        self._ensure_open()
        return self._runs.list(
            status=status,
            task_type=task_type,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )

    def get_events(self, run_id: str, *, limit: int | None = None, offset: int = 0) -> list[Event]:
        """Return a run's events ordered by ``sequence`` ascending."""
        self._ensure_open()
        return self._events.get_by_run(run_id, limit=limit, offset=offset)

    def get_error(self, error_id: str) -> ErrorRecord | None:
        """Load an error record by id, or ``None`` when it does not exist."""
        self._ensure_open()
        return self._errors.get(error_id)

    def get_errors(
        self,
        run_id: str,
        *,
        resolved: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ErrorRecord]:
        """Return a run's error records ordered by creation time."""
        self._ensure_open()
        return self._errors.get_by_run(run_id, resolved=resolved, limit=limit, offset=offset)

    def get_recoveries(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[RecoveryRecord]:
        """Return a run's recovery attempts ordered by start time."""
        self._ensure_open()
        return self._recoveries.get_by_run(run_id, limit=limit, offset=offset)

    def get_verifications(
        self,
        run_id: str,
        *,
        passed: bool | None = None,
        required: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[VerificationRecord]:
        """Return a run's verification verdicts in the order they were recorded."""
        self._ensure_open()
        return self._verifications.get_by_run(
            run_id, passed=passed, required=required, limit=limit, offset=offset
        )

    def get_verification_summary(self, run_id: str) -> VerificationSummary:
        """Aggregate a run's verdicts.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        self._ensure_open()
        return self._verification_engine.summarize(run_id)

    def verified_success(self, run_id: str) -> bool:
        """Whether a run is an agent success **and** independently confirmed.

        Recomputes from the stored facts on every call; nothing caches it onto the
        run, so a verdict recorded later is never ignored.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        self._ensure_open()
        return self._verification_engine.verified_success(run_id)

    def verify(
        self,
        run_id: str,
        verifier: Verifier,
        *,
        context: VerificationContext | None = None,
        required: bool | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> VerificationRecord:
        """Verify an *existing* run, by id.

        The realistic post-hoc entry point: the agent process has already finished
        (and may have exited), and a separate verification step comes along later.
        ``RunContext.verify`` is the same path with the context already in hand.

        Raises:
            RecordNotFoundError: no such run exists.
            VerificationError: not a verifier, or it returned a non-verdict.
        """
        self._ensure_open()
        run = self._runs.get(run_id)
        if run is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        return RunContext(self, run).verify(
            verifier, context=context, required=required, metadata=metadata
        )

    # -- experience --------------------------------------------------------

    def distill_run(
        self,
        run_id: str,
        *,
        explicit_high_value: bool = False,
    ) -> Experience | None:
        """Distil ``run_id`` into an experience, or ``None`` if it is not worth it.

        Idempotent: distilling the same run again returns the experience it already
        belongs to.

        Raises:
            RecordNotFoundError: no such run exists.
            DistillationError: no distillation provider is configured, or the
                provider returned something unusable. Nothing is persisted.
        """
        self._ensure_open()
        return self._experience_service.distill_run(run_id, explicit_high_value=explicit_high_value)

    def evaluate_distillation(
        self,
        run_id: str,
        *,
        explicit_high_value: bool = False,
    ) -> DistillationDecision:
        """Ask whether ``run_id`` would be distilled, without distilling it.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        self._ensure_open()
        return self._experience_service.evaluate(run_id, explicit_high_value=explicit_high_value)

    def get_run_evidence(self, run_id: str) -> RunEvidence:
        """Everything AER knows about ``run_id``, as one immutable package.

        The input a distillation pass sees, exposed for inspection: it is how a
        caller checks *why* an experience says what it says.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        self._ensure_open()
        return self._experience_service.evidence.build(run_id)

    def get_experience(self, experience_id: str) -> Experience | None:
        """Load an experience by id, or ``None`` when it does not exist."""
        self._ensure_open()
        return self._experiences.get(experience_id)

    def list_experiences(
        self,
        *,
        kind: ExperienceKind | None = None,
        domain: str | None = None,
        status: ExperienceStatus | None = None,
        include_deprecated: bool = False,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[Experience]:
        """List stored experiences, newest first by default."""
        self._ensure_open()
        return self._experiences.list(
            kind=kind,
            domain=domain,
            status=status,
            include_deprecated=include_deprecated,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )

    def find_experiences(
        self,
        *,
        kind: ExperienceKind,
        domain: str,
        title: str,
        problem: str,
    ) -> list[Experience]:
        """Experiences with the same fingerprint as the given claim.

        Exposes the deduplication rule at the facade so a caller can ask "do we
        already know this?" without distilling anything.
        """
        self._ensure_open()
        from aer.experience.dedup import dedup_key_for

        key = dedup_key_for(kind=kind, domain=domain, title=title, problem=problem)
        return self._experiences.find_by_dedup_key(key)

    def get_experience_sources(self, experience_id: str) -> list[ExperienceSource]:
        """Provenance links of an experience, oldest first."""
        self._ensure_open()
        return self._experience_sources.list_for_experience(experience_id)

    def get_experiences_for_run(self, run_id: str) -> list[Experience]:
        """Experiences that ``run_id`` supports (at most one today)."""
        self._ensure_open()
        found: list[Experience] = []
        for experience_id in self._experience_sources.get_experiences_for_run(run_id):
            experience = self._experiences.get(experience_id)
            if experience is not None:
                found.append(experience)
        return found

    # -- tracked retrieval (Milestone 7) ----------------------------------

    def retrieve_for_run(
        self,
        query: str,
        *,
        run_id: str | None = None,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = DEFAULT_RETRIEVAL_LIMIT,
        include_deprecated: bool = False,
        experiment_id: str | None = None,
        assignment: SessionAssignment = SessionAssignment.NONE,
        metadata: Mapping[str, object] | None = None,
    ) -> TrackedRetrievalResult:
        """Retrieve **and record** that the retrieval happened.

        The tracked counterpart of :meth:`retrieve`, which stays a pure function
        (round-7 brief, sections 15-17). Use this when the answer is going to an
        agent and the question "did this experience ever actually get used?" is one
        you intend to answer later; use :meth:`retrieve` for dashboards, debugging and
        tests, none of which should inflate the usage statistics.

        A zero-result search still records a session (section 7): "we looked and found
        nothing" is the fact that identifies a gap in the knowledge base, and it is
        invisible if only successful searches are stored.

        Raises:
            RecordNotFoundError: ``run_id`` names no known run.
            KnowledgeIndexUnavailable: the index cannot be reached. Reported rather
                than downgraded to an empty result (section 85).
            KnowledgeQueryError: ``query`` has no searchable text.
            UsageTrackingError: retrieval succeeded but the tracking write failed. The
                caller must not treat this retrieval as tracked (section 51).
        """
        self._ensure_open()
        return self.usage_tracking.retrieve_for_run(
            query,
            run_id=run_id,
            domain=domain,
            mode=mode,
            limit=limit,
            include_deprecated=include_deprecated,
            experiment_id=experiment_id,
            assignment=assignment,
            metadata=metadata,
        )

    def record_injection(
        self,
        *,
        session_id: str,
        experience_ids: Sequence[str],
        context_fingerprint: str | None = None,
        formatter_version: str | None = None,
        positions: Mapping[str, int] | None = None,
        char_counts: Mapping[str, int] | None = None,
        injected_at: datetime | None = None,
    ) -> tuple[ExperienceUsage, ...]:
        """Record which of a result's experiences entered the agent's context.

        Retrieval and injection are separate facts on purpose: a result nobody
        rendered is not use (section 2). Experiences outside the session's result are
        refused, a repeated injection is a no-op, and the rendered context itself is
        never stored -- only a fingerprint (sections 19, 20 and 52).

        Raises:
            RecordNotFoundError: the session does not exist.
            UsageTrackingError: an experience was not part of this session, an
                injection contradicts an existing one, or the write failed.
        """
        self._ensure_open()
        return self.usage_tracking.record_injection(
            session_id=session_id,
            experience_ids=experience_ids,
            context_fingerprint=context_fingerprint,
            formatter_version=formatter_version,
            positions=positions,
            char_counts=char_counts,
            injected_at=injected_at,
        )

    def record_usage_signal(
        self,
        *,
        session_id: str,
        experience_id: str,
        signal: UsageSignal,
        source: UsageSignalSource | None = None,
        at: datetime | None = None,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether an agent adopted, ignored or explicitly rejected an experience.

        ``UNKNOWN`` may become anything; an established signal may only change with
        ``override=True`` (section 53). Never inferred from behaviour: AER does not
        guess adoption from a similar tool call (section 22).

        Raises:
            RecordNotFoundError: the session, or the row, does not exist.
            UsageTrackingError: the signal and its source disagree, or the change
                contradicts an existing signal without ``override``.
        """
        self._ensure_open()
        return self.usage_tracking.record_usage_signal(
            session_id=session_id,
            experience_id=experience_id,
            signal=signal,
            source=source,
            at=at,
            override=override,
            metadata=metadata,
        )

    def record_utility(
        self,
        *,
        session_id: str,
        experience_id: str,
        label: UtilityLabel,
        source: UtilitySource | None = None,
        at: datetime | None = None,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether an experience helped, once that can be judged.

        Separate from :meth:`record_usage_signal` and never derived from it:
        ``ADOPTED`` is what the agent did, ``HELPFUL`` is what happened, and an
        adopted experience can still be wrong (sections 12 and 14).

        Raises:
            RecordNotFoundError: the session, or the row, does not exist.
            UsageTrackingError: the label and its source disagree, or the change
                contradicts an existing label without ``override``.
        """
        self._ensure_open()
        return self.usage_tracking.record_utility(
            session_id=session_id,
            experience_id=experience_id,
            label=label,
            source=source,
            at=at,
            override=override,
            metadata=metadata,
        )

    def attach_session_to_run(
        self,
        session_id: str,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> RetrievalSession:
        """Link a retrieval recorded before its run existed.

        Idempotent for the same run; refuses to re-point an attached session, because
        every usage row beneath it would be reattributed (section 24).

        Raises:
            RecordNotFoundError: the session or the run does not exist.
            UsageTrackingError: the session is attached to a different run.
        """
        self._ensure_open()
        return self.usage_tracking.attach_session_to_run(session_id, run_id, reason=reason)

    def get_retrieval_session(self, session_id: str) -> RetrievalSession | None:
        """Load a retrieval session by id, or ``None`` when it does not exist."""
        self._ensure_open()
        return self._retrieval_sessions.get(session_id)

    def list_retrieval_sessions(
        self,
        *,
        run_id: str | None = None,
        query_fingerprint: str | None = None,
        domain: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[RetrievalSession]:
        """List recorded retrievals, newest first by default."""
        self._ensure_open()
        return self._retrieval_sessions.list(
            run_id=run_id,
            query_fingerprint=query_fingerprint,
            domain=domain,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )

    def get_experience_usage(self, session_id: str, experience_id: str) -> ExperienceUsage | None:
        """One usage row, or ``None`` when that pair was never recorded."""
        self._ensure_open()
        return self._experience_usage.get_by_session_and_experience(session_id, experience_id)

    def get_session_usage(self, session_id: str) -> list[ExperienceUsage]:
        """Every usage row of one session, in the order the agent saw them."""
        self._ensure_open()
        return self._experience_usage.get_by_session(session_id)

    def list_experience_usage(
        self,
        experience_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[ExperienceUsage]:
        """One experience's usage history, newest first by default."""
        self._ensure_open()
        return self._experience_usage.list_for_experience(
            experience_id, limit=limit, offset=offset, newest_first=newest_first
        )

    # -- effectiveness, confidence and promotion (Milestone 7) ------------

    def experience_effectiveness(self, experience_id: str) -> ExperienceEffectivenessReport:
        """Observed usage evidence for one experience.

        An observation, never a causal claim: ``observed_success_rate`` is
        ``P(success | injected)``, which is not ``P(success | do(injected))``
        (sections 28-30).

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        self._ensure_open()
        return self._effectiveness.report(experience_id)

    def experience_effectiveness_all(
        self, *, limit: int = 1000
    ) -> list[ExperienceEffectivenessReport]:
        """Reports for every experience with recorded usage, in a few queries."""
        self._ensure_open()
        return self._effectiveness.report_all(limit=limit)

    def experience_confidence(self, experience_id: str) -> ExperienceConfidence:
        """Deterministic confidence, computed on demand and never stored.

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        self._ensure_open()
        return self._confidence.compute(experience_id)

    def evaluate_promotion(
        self,
        experience_id: str,
        *,
        policy: PromotionPolicy | None = None,
    ) -> PromotionDecision:
        """Ask whether an experience may be promoted, without promoting it.

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        self._ensure_open()
        return self._promotion.evaluate(experience_id, policy=policy)

    def promote_experience(
        self,
        experience_id: str,
        *,
        policy: PromotionPolicy | None = None,
    ) -> Experience | None:
        """Apply ``VERIFIED -> REUSED`` or ``REUSED -> PROVEN`` if the policy allows.

        Returns ``None`` when nothing was eligible, which is the ordinary outcome for
        most experiences.

        The store is committed first and the knowledge index is updated afterwards on
        a best-effort basis: a status change alters what retrieval must *say* about an
        experience, so a stale projection would keep serving the old trust level, but
        a projection failure never invalidates the committed fact (section 44).

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        self._ensure_open()
        updated = self._promotion.promote(experience_id, policy=policy)
        if updated is None:
            return None
        self._project_after_write(experience_id)
        return updated

    def _project_after_write(self, experience_id: str) -> None:
        """Best-effort projection of a committed experience change.

        Only failures of the *projection* are absorbed, and always loudly. The
        SQLite write is already committed and is the durable fact; the index is a
        rebuildable copy, so a failure here means "stale", which
        :meth:`knowledge_status` reports and :meth:`rebuild_knowledge` repairs.
        """
        try:
            self.knowledge_projector.project_experience(experience_id)
        except KnowledgeError as exc:
            logger.warning(
                "projecting experience %s after a promotion failed (%s); the store is "
                "correct and the knowledge index is stale until a rebuild",
                experience_id,
                exc,
            )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Dispose the connection pool and mark this runtime as closed.

        Closing is final: every subsequent operation raises :class:`AERError`, so
        lifecycle mistakes surface immediately instead of against a half-torn-down
        runtime. Re-open the same directory by constructing a new ``AER``.
        """
        if self._closed:
            return
        # The knowledge index is closed first and only if it was ever opened: a
        # runtime that never retrieved anything must not be made to import the graph
        # engine just so it can be shut down. Its failure to close is reported and
        # does not stop the store from being disposed -- losing a lock on a
        # projection is recoverable, losing the store is not.
        if not self._knowledge_injected and self._knowledge_index is not None:
            try:
                self._knowledge_index.close()
            except Exception as exc:
                logger.warning("closing the knowledge index failed: %s", exc)
        self._database.dispose()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise AERError(f"AER runtime for {self._data_dir.as_posix()!r} is already closed")

    def __enter__(self) -> AER:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
