"""Driving AER from translated external events.

    vendor payload ──adapter──▶ envelopes ──ingestor──▶ RunContext
                                    │            │
                                    │            ├─ dedup ledger (adapter_events)
                                    │            ├─ session mapping (adapter_sessions)
                                    │            └─ external event SDK (RunContext.external)
                                    └─ validated against the protocol

The ingestor is the AER side of the adapter protocol, and it is deliberately the only
one. An adapter translates; this decides what a translation *means*. That split is why
a third-party adapter can be wrong about a vendor's format without being able to be
wrong about AER: it cannot reach a repository, cannot open a run, cannot end a run, and
cannot write a verdict (round-8 brief, section 10).

Three responsibilities, in the order they matter:

**Sessions.** An external Agent session is opened, resumed or explicitly reopened
(sections 20-21). The mapping is persisted, so reconnecting after a process restart
finds the run that was already in progress instead of starting a second one. A session
whose run has finished is *refused* unless the caller says ``reopen``, because
"resume" and "a new episode of work" are different intentions and only the caller
knows which it means (section 43).

**Idempotency.** A vendor's event is claimed in the ledger before it is applied, so a
retried delivery is recognised and dropped rather than applied twice (sections 18-19).
Claiming first makes the ledger at-most-once: a crash between claim and apply loses
that event, which is visible as an unapplied row, and that is a deliberate trade --
a doubled event corrupts a statistic silently, while a missing one does not.

**Capability gating.** An adapter that declared it cannot observe adoption is refused
when it tries to record adoption (section 12). The declaration is not documentation;
it is enforced here, because a guessed ``ADOPTED`` eventually becomes training data.

Everything recorded on the way through is sanitized as untrusted external data
(section 25) and capped in size (section 27) before it reaches a run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aer.adapter.protocol import (
    AER_ADAPTER_PROTOCOL_VERSION,
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterSessionRequest,
    AgentAdapter,
    AgentExecutionEnvelope,
    AgentIdentity,
)
from aer.adapter.sanitize import sanitize_external_body
from aer.exceptions import (
    AdapterCapabilityError,
    AdapterError,
    AdapterProtocolError,
    AdapterSessionTerminated,
    RecordNotFoundError,
    UnsupportedAdapterEvent,
)
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.runtime.enums import (
    AdapterIngestOutcome,
    AdapterSessionOutcome,
    EventType,
    RetrievalMode,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)
from aer.runtime.models import (
    AdapterEventRecord,
    AdapterSession,
    ExperienceUsage,
    Run,
    VerificationRecord,
)
from aer.runtime.run import RunContext
from aer.runtime.serialization import JsonObject, to_json_object, utc_now
from aer.storage.repositories import (
    AdapterEventRepository,
    AdapterSessionRepository,
)
from aer.usage.effectiveness import ExperienceEffectivenessReport
from aer.usage.fingerprints import context_fingerprint
from aer.usage.tracking import TrackedRetrievalResult
from aer.verification.base import VerificationContext, Verifier

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from aer.adapter.registry import AdapterRegistry
    from aer.runtime.runtime import AER

__all__ = [
    "AdapterIngestor",
    "AdapterSessionHandle",
    "IngestResult",
]

#: Default message used when a source reports a failure without describing it.
#:
#: A placeholder rather than a guess at the cause: the vendor said something failed and
#: nothing more, and inventing a cause would be inventing evidence. The classification
#: string records how it was obtained.
_UNDESCRIBED_FAILURE = "the source reported a failure without describing it"


@dataclass(frozen=True, slots=True)
class AdapterSessionHandle:
    """A live binding between an external Agent session and one AER run.

    Returned by :meth:`AdapterIngestor.open` and passed back on every subsequent call.
    It carries the adapter that produced it so the caller cannot accidentally drive two
    integrations into one run, and the capabilities it declared so the gates below can
    be enforced without re-asking.
    """

    adapter: AgentAdapter
    identity: AgentIdentity
    capabilities: AdapterCapabilities

    run_id: str
    provider: str
    external_session_id: str
    outcome: AdapterSessionOutcome
    protocol_version: str = AER_ADAPTER_PROTOCOL_VERSION

    @property
    def adapter_name(self) -> str:
        """Name of the adapter driving this session."""
        return self.adapter.name

    @property
    def resumed(self) -> bool:
        """Whether this call reused a run that was already in progress."""
        return self.outcome is AdapterSessionOutcome.RESUMED

    def describe(self) -> str:
        """One line, for a log or a debug command."""
        return (
            f"{self.adapter_name} {self.provider}/{self.external_session_id} "
            f"-> run {self.run_id} ({self.outcome.value})"
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What delivering one external event did.

    ``outcome`` is the answer to the only question a hook cares about: was this
    delivery new? ``aer_event_id`` is filled when the event produced exactly one AER
    event, which is the common case and the one a caller logging the mapping wants.
    """

    outcome: AdapterIngestOutcome
    run_id: str
    external_event_id: str | None = None
    event_type: EventType | None = None
    aer_event_id: int | None = None
    envelopes: int = 0

    @property
    def applied(self) -> bool:
        """Whether anything was written."""
        return self.outcome is AdapterIngestOutcome.APPLIED


class AdapterIngestor:
    """Applies translated external events to AER runs.

    Constructed with the runtime and the two protocol repositories. It never reaches
    into storage for anything else: the run, the events and the error pipeline are
    reached through :class:`~aer.runtime.run.RunContext`, so the terminal-state guard
    and the single error pipeline still apply (section 10).
    """

    def __init__(
        self,
        *,
        runtime: AER,
        sessions: AdapterSessionRepository,
        events: AdapterEventRepository,
        registry: AdapterRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._events = events
        self._registry = registry
        self._clock = clock if callable(clock) else utc_now

    # -- introspection -----------------------------------------------------

    @property
    def protocol_version(self) -> str:
        """The protocol version this ingestor enforces."""
        return AER_ADAPTER_PROTOCOL_VERSION

    def __repr__(self) -> str:
        return f"AdapterIngestor(protocol={self.protocol_version!r})"

    # -- session lifecycle -------------------------------------------------

    def open(
        self,
        adapter: AgentAdapter | str,
        raw: Mapping[str, object],
        *,
        task_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AdapterSessionHandle:
        """Open or resume the AER run for an external Agent session.

        Idempotent for a run that is still in progress: the same external session
        reconnecting gets the run it was already reporting into, never a second one
        (section 21).

        Args:
            adapter: The adapter, or the name of one registered in the registry.
            raw: The vendor's session-start payload.
            task_type: AER-side task category, when the caller knows it.
            metadata: Extra run metadata.

        Returns:
            A handle reporting whether the run was ``STARTED`` or ``RESUMED``.

        Raises:
            AdapterError: no adapter is registered under that name.
            AdapterProtocolError: the adapter speaks another protocol version, or its
                translation is unusable (no session id, no task).
            AdapterSessionTerminated: the session's run has already finished. Use
                :meth:`reopen` to say explicitly that a new episode is starting
                (section 43).
        """
        return self._open(adapter, raw, task_type=task_type, metadata=metadata, reopen=False)

    def reopen(
        self,
        adapter: AgentAdapter | str,
        raw: Mapping[str, object],
        *,
        task_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AdapterSessionHandle:
        """Start a **new** run for an external session whose previous run finished.

        The explicit form of "this is a second episode, not a resume". The previous
        run ids are kept on the mapping, so the fact that one external session produced
        two AER runs is recorded rather than overwritten (section 20).
        """
        return self._open(adapter, raw, task_type=task_type, metadata=metadata, reopen=True)

    def close(
        self,
        handle: AdapterSessionHandle,
        raw: Mapping[str, object],
    ) -> Run | None:
        """Translate a session-end payload and finish the run, if the source said how.

        Returns ``None`` when the adapter reported no terminal status at all: the run
        stays ``RUNNING``, because "the session disconnected" and "the task ended" are
        different facts and only the second one may close a trace.

        An adapter whose platform *has* ended the session but declared no outcome should
        report ``INCONCLUSIVE`` rather than ``None``: the run is over either way, and
        closing it with a status that claims nothing keeps it available to verification
        instead of leaving it running forever (round-8.1.1, D-100).

        Idempotent: closing an already-finished run is a no-op, so a duplicated
        session-end delivery cannot fail a hook.
        """
        request = self._translate_finish(handle, raw)
        run = self._runtime.get_run(handle.run_id)
        if run is None:  # pragma: no cover - the mapping cascades with the run
            raise RecordNotFoundError(f"Run not found: {handle.run_id}")
        if run.is_finished:
            return run
        if request.status is None:
            return None

        context = RunContext(self._runtime, run)
        extra = sanitize_external_body(request.metadata).body if request.metadata else {}
        if request.reason is not None or extra:
            merged = {
                **run.metadata,
                **extra,
                **({"adapter_close_reason": request.reason} if request.reason else {}),
            }
            run.metadata = to_json_object(merged)
            self._runtime.runs.update(run)
        context.finish(request.status)
        return self._runtime.get_run(handle.run_id)

    # -- events ------------------------------------------------------------

    def ingest(
        self,
        handle: AdapterSessionHandle,
        raw: Mapping[str, object],
    ) -> IngestResult:
        """Translate one vendor event and apply it to the session's run.

        The whole external event is translated first, validated, and then either
        applied or rejected as a duplicate. Nothing is written before the ledger claim,
        so a retried delivery changes nothing at all (section 18).

        Args:
            handle: The session the event belongs to.
            raw: The vendor's payload.

        Returns:
            An :class:`IngestResult` whose outcome is ``APPLIED``, ``DUPLICATE`` or
            ``IGNORED``.

        Raises:
            AdapterError: the adapter crashed while translating. The run is left
                untouched -- an integration bug must not look like an agent failure
                (section 28).
            AdapterProtocolError: the translation is unusable, or its envelopes
                disagree about which external event they came from.
            AdapterCapabilityError: the event needs a capability the adapter declared
                it does not have (section 12).
            UnsupportedAdapterEvent: an envelope carries an event type only the
                lifecycle or the verification engine may write.
            RunStateError: the run already finished and the event is not an
                observation (section 22).
        """
        envelopes = self._translate(handle, raw)
        if not envelopes:
            return IngestResult(
                outcome=AdapterIngestOutcome.IGNORED, run_id=handle.run_id, envelopes=0
            )

        external_event_id = self._single_external_id(handle, envelopes)
        duplicate, claim = self._claim(handle, envelopes, external_event_id)
        if duplicate:
            return IngestResult(
                outcome=AdapterIngestOutcome.DUPLICATE,
                run_id=handle.run_id,
                external_event_id=external_event_id,
                event_type=envelopes[0].event_type,
                envelopes=len(envelopes),
            )

        context = self._context_for(handle)
        applied_id: int | None = None
        for envelope in envelopes:
            event_id = self._apply(handle, context, envelope)
            if applied_id is None:
                applied_id = event_id
        # If applying raised, the claim deliberately stays unapplied: the event is
        # treated as delivered-and-lost rather than retried into a duplicate. The
        # module docstring explains why that is the safer half of the trade.
        if claim is not None:
            self._events.mark_applied(claim.id, aer_event_id=applied_id)
        return IngestResult(
            outcome=AdapterIngestOutcome.APPLIED,
            run_id=handle.run_id,
            external_event_id=external_event_id,
            event_type=envelopes[0].event_type,
            aer_event_id=applied_id,
            envelopes=len(envelopes),
        )

    # -- adapter-facing usage operations -----------------------------------

    def retrieve(
        self,
        handle: AdapterSessionHandle,
        query: str,
        *,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = 3,
        include_deprecated: bool = False,
    ) -> TrackedRetrievalResult:
        """Retrieve experience for this session's run, and record the retrieval.

        The run id comes from the handle, so an adapter cannot accidentally attribute a
        retrieval to a different task. Without ``run_id`` the retrieval still records
        itself, but it lands unattributed and cannot contribute to effectiveness
        (round-7 brief, section 24).
        """
        return self._runtime.retrieve_for_run(
            query,
            run_id=handle.run_id,
            domain=domain,
            mode=mode,
            limit=limit,
            include_deprecated=include_deprecated,
        )

    def inject(
        self,
        handle: AdapterSessionHandle,
        tracked: TrackedRetrievalResult,
        *,
        experience_ids: Sequence[str] | None = None,
        formatter: ExperienceContextFormatter | None = None,
        formatter_version: str = FORMATTER_VERSION,
        context: str | None = None,
    ) -> tuple[ExperienceUsage, ...]:
        """Record which retrieved experiences actually entered the agent's context.

        The adapter is the only component that knows this, which makes it the
        authoritative caller of injection tracking (section 14). The character counts
        and the context fingerprint are computed here from the rendered form rather
        than accepted from the caller: dividing a total by the number of hits, or
        fingerprinting something other than what was rendered, produces plausible
        numbers that mean nothing.

        Args:
            handle: The session, for provenance only -- the run id is not needed here.
            tracked: The retrieval whose result was rendered.
            experience_ids: The subset actually injected. ``None`` means all of them.
            formatter: The renderer used, when the caller did not use the default one.
            formatter_version: Which renderer produced the text.
            context: The rendered context, when the caller has it. Supplying it makes
                the recorded fingerprint describe the real text instead of a
                re-rendering of it.
        """
        self._require_retrieval_of_session(handle, tracked)
        renderer = formatter or ExperienceContextFormatter()
        ordered = list(tracked.result.all_hits)
        ranked = {hit.experience_id: rank for rank, hit in enumerate(ordered, start=1)}
        wanted = (
            {hit.experience_id for hit in ordered}
            if experience_ids is None
            else set(experience_ids)
        )
        selected = [hit for hit in ordered if hit.experience_id in wanted]
        rendered = context if context is not None else renderer.format(tracked.result)

        return self._runtime.record_injection(
            session_id=tracked.session_id,
            experience_ids=[hit.experience_id for hit in selected],
            context_fingerprint=context_fingerprint(rendered),
            formatter_version=formatter_version,
            positions={hit.experience_id: ranked[hit.experience_id] for hit in selected},
            char_counts={hit.experience_id: len(renderer.format_hit(hit)) for hit in selected},
        )

    def record_usage_signal(
        self,
        handle: AdapterSessionHandle,
        tracked: TrackedRetrievalResult,
        *,
        experience_id: str,
        signal: UsageSignal,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether the agent used an experience, with the adapter as the source.

        Refused unless the adapter declared it can observe this (section 12). A
        platform that cannot tell you whether the agent used what it was given must
        leave the row at ``UNKNOWN``, because a guessed ``ADOPTED`` is indistinguishable
        from an observed one once it is stored -- and it is the stronger claim.

        The source is always ``ADAPTER``: AER is recording what an integration observed,
        and calling it anything else would overstate how directly the agent spoke.
        """
        self._require(handle, "explicit_adoption_signal", what="usage signals")
        return self._runtime.record_usage_signal(
            session_id=tracked.session_id,
            experience_id=experience_id,
            signal=signal,
            source=UsageSignalSource.ADAPTER,
            override=override,
            metadata=metadata,
        )

    def record_utility(
        self,
        handle: AdapterSessionHandle,
        tracked: TrackedRetrievalResult,
        *,
        experience_id: str,
        label: UtilityLabel,
        source: UtilitySource = UtilitySource.HUMAN,
        override: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperienceUsage:
        """Record whether an experience **helped**, when the platform can say so.

        Gated separately from adoption, and off by default, because the two are
        different claims: a task that succeeded after an experience was adopted is not
        evidence that the experience caused the success (round-8 sections 16 and 34).
        An adapter is never allowed to write ``HELPFUL`` merely because the run ended
        well.
        """
        self._require(handle, "explicit_utility_signal", what="utility labels")
        return self._runtime.record_utility(
            session_id=tracked.session_id,
            experience_id=experience_id,
            label=label,
            source=source,
            override=override,
            metadata=metadata,
        )

    def verify(
        self,
        handle: AdapterSessionHandle,
        verifier: Verifier,
        *,
        payload: Mapping[str, object] | None = None,
        required: bool | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> VerificationRecord:
        """Hand external evidence to a real verifier and let the engine record it.

        The adapter's role ends at supplying evidence. It cannot declare a verdict,
        because a verdict written by the integration that is being evaluated is not a
        verification (section 39); the engine, the verifier and the ``required`` flag
        decide what the evidence means.
        """
        self._require(handle, "external_verification", what="verification input")
        run = self._runtime.get_run(handle.run_id)
        if run is None:  # pragma: no cover - the handle was built from a live run
            raise RecordNotFoundError(f"Run not found: {handle.run_id}")
        context = VerificationContext(
            run_id=handle.run_id,
            task=run.task_description,
            payload=to_json_object(payload),
            metadata=to_json_object(metadata),
        )
        return self._runtime.verify(handle.run_id, verifier, context=context, required=required)

    # -- read helpers ------------------------------------------------------

    def session_for(self, provider: str, external_session_id: str) -> AdapterSession | None:
        """The persisted mapping for an external session, or ``None``.

        The reconnect lookup, exposed so an integration can ask "do I already have a
        run?" without opening anything.
        """
        return self._sessions.find(provider, external_session_id)

    def effectiveness(self, experience_id: str) -> ExperienceEffectivenessReport:
        """The observed effectiveness of an experience, as M7 defines it."""
        return self._runtime.experience_effectiveness(experience_id)

    # -- session plumbing --------------------------------------------------

    def _open(
        self,
        adapter: AgentAdapter | str,
        raw: Mapping[str, object],
        *,
        task_type: str | None,
        metadata: Mapping[str, object] | None,
        reopen: bool,
    ) -> AdapterSessionHandle:
        resolved = self._resolve(adapter)
        request = self._translate_start(resolved, raw)
        identity = resolved.identity()
        capabilities = resolved.capabilities()

        existing = self._sessions.find(identity.provider, request.external_session_id)
        if existing is not None:
            run = self._runtime.get_run(existing.aer_run_id)
            if run is not None and not run.is_finished:
                return AdapterSessionHandle(
                    adapter=resolved,
                    identity=identity,
                    capabilities=capabilities,
                    run_id=run.id,
                    provider=identity.provider,
                    external_session_id=request.external_session_id,
                    outcome=AdapterSessionOutcome.RESUMED,
                )
            if not reopen:
                status = "missing" if run is None else run.status.value
                raise AdapterSessionTerminated(
                    f"External session {identity.provider}/"
                    f"{request.external_session_id} is already mapped to run "
                    f"{existing.aer_run_id} ({status}). Call reopen() to start a new "
                    "episode, or open() a different session."
                )
            return self._start_new_run(
                resolved,
                identity,
                capabilities,
                request,
                task_type=task_type,
                metadata=metadata,
                outcome=AdapterSessionOutcome.REOPENED,
                previous=existing,
            )

        return self._start_new_run(
            resolved,
            identity,
            capabilities,
            request,
            task_type=task_type,
            metadata=metadata,
            outcome=AdapterSessionOutcome.STARTED,
            previous=None,
        )

    def _start_new_run(
        self,
        adapter: AgentAdapter,
        identity: AgentIdentity,
        capabilities: AdapterCapabilities,
        request: AdapterSessionRequest,
        *,
        task_type: str | None,
        metadata: Mapping[str, object] | None,
        outcome: AdapterSessionOutcome,
        previous: AdapterSession | None,
    ) -> AdapterSessionHandle:
        """Create the run and its mapping, or re-point an existing mapping at it."""
        extra = sanitize_external_body(metadata).body if metadata else {}
        run_metadata: JsonObject = {
            **extra,
            "adapter": {
                "name": identity.adapter_name,
                "version": identity.adapter_version,
                "protocol_version": adapter.protocol_version,
            },
            "provider": identity.provider,
            "agent": {
                "name": identity.agent_name,
                "version": identity.agent_version,
                "model": identity.model,
                "model_version": identity.model_version,
            },
            "external_session_id": request.external_session_id,
        }
        if request.external_run_id is not None:
            run_metadata["external_run_id"] = request.external_run_id

        context = self._runtime.start_run(
            task=request.task,
            task_type=request.task_type if request.task_type is not None else task_type,
            agent_name=identity.agent_name,
            agent_version=identity.agent_version,
            model_provider=identity.provider,
            model_name=identity.model,
            metadata=run_metadata,
        )

        self._persist_session(
            identity, request, context.run_id, adapter.protocol_version, previous=previous
        )
        return AdapterSessionHandle(
            adapter=adapter,
            identity=identity,
            capabilities=capabilities,
            run_id=context.run_id,
            provider=identity.provider,
            external_session_id=request.external_session_id,
            outcome=outcome,
        )

    def _persist_session(
        self,
        identity: AgentIdentity,
        request: AdapterSessionRequest,
        run_id: str,
        protocol_version: str,
        *,
        previous: AdapterSession | None,
    ) -> None:
        """Write the mapping: a fresh row, or an existing one re-pointed.

        Called for its effect only -- the handle carries what the caller needs, and the
        mapping is read back from storage on a reconnect, which is the whole point of
        persisting it.
        """
        now = self._clock()
        if previous is None:
            self._sessions.create(
                AdapterSession(
                    provider=identity.provider,
                    external_session_id=request.external_session_id,
                    adapter_name=identity.adapter_name,
                    external_run_id=request.external_run_id,
                    aer_run_id=run_id,
                    protocol_version=protocol_version,
                    metadata=to_json_object(request.metadata),
                )
            )
            return

        # Reopening: keep the history, do not rewrite it.
        history = list(previous.previous_runs)
        history.append(previous.aer_run_id)
        self._sessions.update(
            previous.model_copy(
                update={
                    "adapter_name": identity.adapter_name,
                    "external_run_id": request.external_run_id,
                    "aer_run_id": run_id,
                    "protocol_version": protocol_version,
                    "updated_at": now,
                    "metadata": {
                        **previous.metadata,
                        "previous_runs": history,
                        "reopened_at": now.isoformat(),
                    },
                }
            )
        )

    # -- translation -------------------------------------------------------

    def _resolve(self, adapter: AgentAdapter | str) -> AgentAdapter:
        """Turn a name into an adapter, checking the protocol version either way."""
        if isinstance(adapter, str):
            if self._registry is None:
                raise AdapterError(
                    f"Cannot resolve adapter {adapter!r} by name: no registry was supplied"
                )
            return self._registry.create(adapter)
        if not isinstance(adapter, AgentAdapter):
            raise AdapterProtocolError(f"{type(adapter).__name__} is not an AgentAdapter")
        if adapter.protocol_version != AER_ADAPTER_PROTOCOL_VERSION:
            raise AdapterProtocolError(
                f"Adapter {adapter.name!r} speaks protocol version "
                f"{adapter.protocol_version!r}; this build speaks "
                f"{AER_ADAPTER_PROTOCOL_VERSION!r}"
            )
        return adapter

    def _translate_start(
        self, adapter: AgentAdapter, raw: Mapping[str, object]
    ) -> AdapterSessionRequest:
        try:
            request = adapter.start(raw)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                f"Adapter {adapter.name!r} failed while translating a session start "
                f"({type(exc).__name__}: {exc}). The integration is broken; no run was "
                "created and nothing was written."
            ) from exc
        if not isinstance(request, AdapterSessionRequest):
            raise AdapterProtocolError(
                f"Adapter {adapter.name!r} returned {type(request).__name__} from "
                "start(); expected AdapterSessionRequest"
            )
        if not request.external_session_id.strip():
            raise AdapterProtocolError(
                f"Adapter {adapter.name!r} produced a session start with no external "
                "session id; a session that cannot be identified cannot be resumed"
            )
        if not request.task.strip():
            raise AdapterProtocolError(
                f"Adapter {adapter.name!r} produced a session start with no task; the "
                "run would have nothing to describe"
            )
        return request

    def _translate_finish(
        self, handle: AdapterSessionHandle, raw: Mapping[str, object]
    ) -> AdapterFinishRequest:
        try:
            request = handle.adapter.finish(raw)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                f"Adapter {handle.adapter_name!r} failed while translating a session end "
                f"({type(exc).__name__}: {exc}). The run was left as it was."
            ) from exc
        if not isinstance(request, AdapterFinishRequest):
            raise AdapterProtocolError(
                f"Adapter {handle.adapter_name!r} returned {type(request).__name__} from "
                "finish(); expected AdapterFinishRequest"
            )
        return request

    def _translate(
        self, handle: AdapterSessionHandle, raw: Mapping[str, object]
    ) -> tuple[AgentExecutionEnvelope, ...]:
        """Run the adapter, translating a crash into an explicit integration error.

        The failure is **not** recorded on the run (section 28): an ``ERROR`` event
        there would be indistinguishable from the agent's own failure, would make the
        distillation policy treat the run as interesting, and would let an integration
        bug become experience. The exception carries the diagnosis instead.
        """
        try:
            envelopes = tuple(handle.adapter.handle_event(raw))
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                f"Adapter {handle.adapter_name!r} failed while translating an event "
                f"({type(exc).__name__}: {exc}). The run was not modified."
            ) from exc

        for envelope in envelopes:
            if not isinstance(envelope, AgentExecutionEnvelope):
                raise AdapterProtocolError(
                    f"Adapter {handle.adapter_name!r} produced "
                    f"{type(envelope).__name__}; expected AgentExecutionEnvelope"
                )
            if envelope.protocol_version != handle.protocol_version:
                raise AdapterProtocolError(
                    f"Envelope declares protocol version {envelope.protocol_version!r}, "
                    f"but the session speaks {handle.protocol_version!r}"
                )
            if envelope.identity.adapter_name != handle.identity.adapter_name:
                raise AdapterProtocolError(
                    f"Envelope identity claims adapter {envelope.identity.adapter_name!r} "
                    f"but the session was opened by {handle.identity.adapter_name!r}"
                )
            envelope.require_ingestable()
        return envelopes

    def _single_external_id(
        self,
        handle: AdapterSessionHandle,
        envelopes: Sequence[AgentExecutionEnvelope],
    ) -> str | None:
        """The one external event id a batch agrees on, or ``None``.

        A vendor event that a translation splits into several envelopes is still **one**
        external event, so the id must be the same on all of them. Disagreement means
        the adapter is treating a derived envelope as a separate delivery, which would
        make the ledger's guarantee meaningless -- so it is refused rather than
        resolved by picking one (section 18).
        """
        ids = {envelope.external_event_id for envelope in envelopes}
        if len(ids) > 1:
            values = sorted(str(item) for item in ids)
            raise AdapterProtocolError(
                f"Adapter {handle.adapter_name!r} produced {len(envelopes)} envelopes "
                f"with {len(ids)} different external_event_id values ({values}). Every "
                "envelope derived from one vendor event must carry that event's id."
            )
        return next(iter(ids))

    # -- applying ----------------------------------------------------------

    def _context_for(self, handle: AdapterSessionHandle) -> RunContext:
        run = self._runtime.get_run(handle.run_id)
        if run is None:  # pragma: no cover - the mapping cascades with the run
            raise RecordNotFoundError(f"Run not found: {handle.run_id}")
        return RunContext(self._runtime, run)

    def _claim(
        self,
        handle: AdapterSessionHandle,
        envelopes: Sequence[AgentExecutionEnvelope],
        external_event_id: str | None,
    ) -> tuple[bool, AdapterEventRecord | None]:
        """Claim the external event, or report that it was already claimed.

        Returns ``(is_duplicate, claim)``. ``(False, None)`` is the third case and the
        reason this is a pair rather than an optional record: an event with no external
        id cannot be claimed at all, so it is applied **without** idempotency -- the
        documented consequence of a platform that gives its events no identity
        (section 18). Conflating that with "already seen" would silently drop every
        event from such a platform.
        """
        if external_event_id is None:
            return False, None

        existing = self._events.find(handle.provider, external_event_id)
        if existing is not None:
            return True, None

        first = envelopes[0]
        claim = self._events.record(
            AdapterEventRecord(
                provider=handle.provider,
                external_event_id=external_event_id,
                adapter_name=handle.adapter_name,
                event_type=first.event_type,
                aer_run_id=handle.run_id,
                applied=False,
                external_sequence=first.external_sequence,
                external_timestamp=first.external_timestamp,
                metadata={
                    "envelopes": len(envelopes),
                    "event_types": [item.event_type.value for item in envelopes],
                    "external_session_id": handle.external_session_id,
                },
            )
        )
        return False, claim

    def _apply(
        self,
        handle: AdapterSessionHandle,
        context: RunContext,
        envelope: AgentExecutionEnvelope,
    ) -> int | None:
        """Write one envelope, returning the AER event id it produced.

        The dispatch table is the whole translation contract in one place: which
        protocol event becomes which runtime call, and what each one requires.
        """
        recorder = context.external(source=f"adapter:{handle.adapter_name}")
        body, meta = self._sanitized_body(handle, envelope)
        event_type = envelope.event_type

        if event_type is EventType.MODEL_CALL:
            return recorder.event(EventType.MODEL_CALL, input=body, metadata=meta).id
        if event_type is EventType.MODEL_RESULT:
            return recorder.event(EventType.MODEL_RESULT, output=body, metadata=meta).id

        if event_type is EventType.TOOL_CALL:
            self._require(handle, "tool_events", what="tool events")
            return recorder.event(EventType.TOOL_CALL, input=body, metadata=meta).id

        if event_type is EventType.TOOL_RESULT:
            self._require(handle, "tool_events", what="tool events")
            if body.get("success") is False:
                recorder.failure(
                    **_failure_details(body, default_type="external.ToolFailure"),
                    metadata=self._tool_metadata(body),
                )
            return recorder.event(EventType.TOOL_RESULT, output=body, metadata=meta).id

        if event_type is EventType.ERROR:
            error_record = recorder.failure(
                **_failure_details(body, default_type="external.AgentError"),
                metadata=meta,
            )
            return error_record.event_id

        if event_type is EventType.RECOVERY_START:
            reason = _required_text(body, "reason", event_type=event_type)
            started = recorder.recovery_started(
                reason,
                error_id=_optional_text(body.get("error_id")),
                metadata=meta,
            )
            return started.start_event_id

        if event_type is EventType.RECOVERY_RESULT:
            success = body.get("success")
            if not isinstance(success, bool):
                raise AdapterProtocolError(
                    "A RECOVERY_RESULT must state success as a boolean; whether a repair "
                    "worked is the one thing this event exists to say"
                )
            recovery_id = _optional_text(body.get("recovery_id")) or self._open_recovery(
                context, event_type=event_type
            )
            finished = recorder.recovery_finished(
                recovery_id,
                success=success,
                outcome=body.get("result"),
                duration_ms=_optional_int(body.get("duration_ms")),
                metadata=meta,
            )
            return finished.result_event_id

        if event_type is EventType.HUMAN_FEEDBACK:
            self._require(handle, "human_feedback", what="human feedback")
            return recorder.event(EventType.HUMAN_FEEDBACK, output=body, metadata=meta).id

        # Unreachable: _translate already refused anything outside the vocabulary.
        raise UnsupportedAdapterEvent(  # pragma: no cover - defensive
            f"{event_type.value} is not a protocol event"
        )

    def _open_recovery(self, context: RunContext, *, event_type: EventType) -> str:
        """Resolve the recovery attempt a result refers to.

        Vendors have never heard of a ``RecoveryRecord``, so a result event cannot name
        one. The oldest still-open attempt is the one that finishes first, which is how
        nested attempts behave in practice; a result with nothing open is a protocol
        error rather than a hint to invent an attempt.
        """
        open_record = self._runtime.recoveries.earliest_open(context.run_id)
        if open_record is None:
            raise AdapterProtocolError(
                f"A {event_type.value} arrived with no open recovery attempt on run "
                f"{context.run_id}. Start the attempt first: an end without a beginning "
                "cannot be recorded as a repair history."
            )
        return open_record.id

    def _sanitized_body(
        self,
        handle: AdapterSessionHandle,
        envelope: AgentExecutionEnvelope,
    ) -> tuple[JsonObject, JsonObject]:
        """Sanitize an envelope's body and metadata, and build the event metadata."""
        body = sanitize_external_body(envelope.body)
        extra = sanitize_external_body(envelope.metadata)
        meta: JsonObject = dict(extra.body)

        meta["adapter"] = {
            "name": handle.adapter_name,
            "adapter_version": handle.identity.adapter_version,
            "protocol_version": handle.protocol_version,
        }
        meta["provider"] = handle.provider
        external: JsonObject = {"session_id": handle.external_session_id}
        if envelope.external_event_id is not None:
            external["event_id"] = envelope.external_event_id
        if envelope.external_sequence is not None:
            external["sequence"] = envelope.external_sequence
        if envelope.external_timestamp is not None:
            external["timestamp"] = envelope.external_timestamp.isoformat()
        meta["external"] = external

        notes = {
            "redacted_keys": sorted(set(body.redacted_keys) | set(extra.redacted_keys)),
            "dropped_private_keys": sorted(set(body.dropped_keys) | set(extra.dropped_keys)),
            "truncated_strings": body.truncated_strings + extra.truncated_strings,
        }
        if (
            any(notes[key] for key in ("redacted_keys", "dropped_private_keys"))
            or notes["truncated_strings"]
        ):
            meta["sanitization"] = to_json_object(notes)
        return body.body, meta

    def _tool_metadata(self, body: JsonObject) -> JsonObject:
        """The little that is worth carrying from a failed tool result to its error."""
        tool = body.get("tool")
        return {"tool": tool} if isinstance(tool, str) else {}

    # -- capability gates --------------------------------------------------

    def _require_retrieval_of_session(
        self, handle: AdapterSessionHandle, tracked: TrackedRetrievalResult
    ) -> None:
        """Refuse to record usage against another run's retrieval.

        An integration that drives more than one session can cross-wire them, and the
        resulting row would be indistinguishable from a real observation: an injection
        attributed to a run that never saw the experience. Checking costs one lookup and
        removes the whole class of mistake.

        A retrieval the caller made *without* a run id is left alone: it is
        unattributed by construction, and M7 already reports those separately.
        """
        session = self._runtime.get_retrieval_session(tracked.session_id)
        if session is None:  # pragma: no cover - the result was just produced
            raise RecordNotFoundError(f"Retrieval session not found: {tracked.session_id}")
        if session.run_id is not None and session.run_id != handle.run_id:
            raise AdapterProtocolError(
                f"Retrieval session {tracked.session_id} belongs to run "
                f"{session.run_id}, but adapter {handle.adapter_name!r} is driving run "
                f"{handle.run_id}. Refusing to record usage against another run."
            )

    def _require(self, handle: AdapterSessionHandle, capability: str, *, what: str) -> None:
        """Refuse a fact the adapter declared it cannot observe (section 12)."""
        if getattr(handle.capabilities, capability):
            return
        raise AdapterCapabilityError(
            f"Adapter {handle.adapter_name!r} did not declare {capability!r}, so it "
            f"cannot record {what}. Missing capabilities: "
            f"{list(handle.capabilities.missing())}. A guessed value is worse than an "
            "honest UNKNOWN, because it is indistinguishable from an observed one."
        )


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------


def _failure_details(body: JsonObject, *, default_type: str) -> dict[str, Any]:
    """Extract a failure description from a body, in any of the shapes vendors use.

    Three accepted forms, because three is what real integrations send: a structured
    ``error`` object, an ``error`` string, and the failure fields at the top level. The
    defaults are documented rather than clever -- a source that says only "it failed"
    gets a classification that says so, instead of a cause AER invented.
    """
    error = body.get("error")
    if isinstance(error, dict):
        error_type = _optional_text(error.get("error_type")) or default_type
        message = (
            _optional_text(error.get("message"))
            or _optional_text(error.get("summary"))
            or _UNDESCRIBED_FAILURE
        )
        stack = _optional_text(error.get("stack_trace")) or _optional_text(error.get("stack"))
        recoverable = error.get("recoverable")
    elif isinstance(error, str) and error.strip():
        error_type = default_type
        message = error
        stack = _optional_text(body.get("stack_trace"))
        recoverable = body.get("recoverable")
    else:
        error_type = _optional_text(body.get("error_type")) or default_type
        message = (
            _optional_text(body.get("message"))
            or _optional_text(body.get("summary"))
            or _UNDESCRIBED_FAILURE
        )
        stack = _optional_text(body.get("stack_trace"))
        recoverable = body.get("recoverable")

    return {
        "error_type": error_type,
        "message": message,
        "stack_trace": stack,
        "recoverable": recoverable if isinstance(recoverable, bool) else True,
    }


def _required_text(body: JsonObject, key: str, *, event_type: EventType) -> str:
    value = _optional_text(body.get(key))
    if value is None:
        raise AdapterProtocolError(
            f"A {event_type.value} must carry a non-empty {key!r}: {key} is the only "
            "thing that makes the event interpretable later"
        )
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None
