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
from collections.abc import Mapping
from pathlib import Path

from aer.exceptions import AERError, RecordNotFoundError
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
from aer.runtime.serialization import new_id, to_json_object
from aer.storage.database import Database
from aer.storage.repositories import (
    ErrorRepository,
    EventRepository,
    ExperienceRepository,
    ExperienceSourceRepository,
    RecoveryRepository,
    RunRepository,
    VerificationRepository,
)
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
        self._verification_engine = VerificationEngine(
            runs=self._runs,
            verifications=self._verifications,
        )
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
