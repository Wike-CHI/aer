"""The Agent Adapter Protocol: the vocabulary every integration speaks.

    Codex / Claude Code / Cursor / DSH / an in-house agent
                        │
                        ▼   (vendor-specific, lives outside AER)
                 Agent-specific Adapter
                        │
                        ▼   (this module)
                 Agent Adapter Protocol
                        │
                        ▼
                    AER Runtime

The one rule this module exists to enforce: **AER Core does not know what a Claude
hook, a Codex event, a Cursor transcript or a DSH session is** (round-8 brief, section
1). It knows about runs, events, executions and observations. An adapter's whole job is
to translate, normalize, sanitize and associate (section 2) -- and nothing else:
distillation, verification policy, ranking, promotion and dataset generation all
belong to AER and stay there.

What crosses this boundary is deliberately small, and deliberately **not** a mirror of
any vendor's format:

* :class:`AgentIdentity` -- who is running, and out of which adapter;
* :class:`AdapterCapabilities` -- what this integration can actually observe. The
  declaration is what stops AER from pretending a platform provides information it
  does not (sections 11-12);
* :class:`AgentAction` / :class:`AgentObservation` -- one normalised shape for "the
  agent did something" and "something happened back" (sections 7-8);
* :class:`AgentExecutionEnvelope` -- one translated event, ready to be applied;
* :class:`AgentAdapter` -- the three translation methods an implementation provides.

Two naming choices differ from the brief on purpose, and both exist to prevent the
same mistake -- reading a vendor's clock or numbering as AER's:

* the brief's ``timestamp`` is :attr:`AgentExecutionEnvelope.external_timestamp`. A
  field called ``timestamp`` on an envelope would be read as authoritative, and an
  Agent with a wrong clock would then be able to corrupt a timeline (section 24);
* the brief's ``sequence`` is :attr:`AgentExecutionEnvelope.external_sequence`, for
  the same reason: AER's own ``sequence`` is arrival order, and a late-delivered
  result must not be able to renumber history (section 23).

Neither is dropped -- both are stored verbatim beside AER's own ordering.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aer.exceptions import UnsupportedAdapterEvent
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.serialization import JsonObject

__all__ = [
    "ADAPTER_EVENT_TYPES",
    "AER_ADAPTER_PROTOCOL_VERSION",
    "AdapterCapabilities",
    "AdapterFinishRequest",
    "AdapterSessionRequest",
    "AgentAction",
    "AgentAdapter",
    "AgentExecutionEnvelope",
    "AgentIdentity",
    "AgentObservation",
    "ObservationKind",
]

#: Version of the protocol, named separately from the package version on purpose.
#:
#: A package version is a release; a protocol version is a wire format. They move at
#: completely different rates, and an adapter author needs to know which one to check
#: against (round-8 brief, section 35). Bump this whenever the *meaning* of an
#: envelope changes, and never when a release merely ships.
AER_ADAPTER_PROTOCOL_VERSION = "1"

#: Event types an envelope may carry into :meth:`AdapterIngestor.ingest`.
#:
#: The complement is refused rather than silently mapped, and each refusal names the
#: route that does exist:
#:
#: * ``TASK_START`` / ``TASK_END`` are the run's lifecycle -- opening and closing a
#:   session drives them, so an adapter cannot have a second, weaker way to end a run;
#: * ``VERIFICATION`` is written only by the verification engine. An adapter may relay
#:   evidence to a real verifier, but it may never declare a pass itself (section 39).
ADAPTER_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.MODEL_CALL,
        EventType.MODEL_RESULT,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.ERROR,
        EventType.RECOVERY_START,
        EventType.RECOVERY_RESULT,
        EventType.HUMAN_FEEDBACK,
    }
)

#: Envelope fields that are structural, and so reserved for the protocol's own use.
_RESPONSIBLY_CLOSED = frozenset({"TASK_START", "TASK_END", "VERIFICATION"})

_FORBIDDEN_EXTRA = ConfigDict(extra="forbid", frozen=True)


class ObservationKind(StrEnum):
    """What came back to the agent (round-8 brief, section 8).

    Four kinds rather than an ``ok: bool``: "the tool returned an error" and "the
    environment answered" are different events, and folding them into a boolean would
    lose the difference between a failed call and a neutral reply.
    """

    TOOL_SUCCESS = "TOOL_SUCCESS"
    TOOL_FAILURE = "TOOL_FAILURE"
    ENVIRONMENT = "ENVIRONMENT"
    HUMAN = "HUMAN"


class AgentIdentity(BaseModel):
    """Who is executing, and through which integration (section 3).

    ``provider`` and ``agent_name`` are the *identity* of the agent; ``model`` is
    descriptive only and must never be used to decide what kind of agent this is
    (section 3). Models are renamed, aliased and swapped under a constant agent, so a
    rule keyed on a model name would silently stop matching one Tuesday.

    ``adapter_name`` / ``adapter_version`` are recorded because the interesting
    operational question later is "is this integration systematically producing
    low-quality data?", and that question cannot be asked without knowing which
    integration produced each row (section 38).
    """

    model_config = _FORBIDDEN_EXTRA

    provider: str
    agent_name: str
    adapter_name: str
    adapter_version: str

    agent_version: str | None = None
    model: str | None = None
    model_version: str | None = None

    #: The vendor's own session identifier. Recorded, never used as a primary key
    #: (section 17).
    session_id: str | None = None
    external_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """What this integration is actually able to observe (section 11).

    Every field defaults to ``False``, and that default is the whole point: an adapter
    author has to *claim* a capability before AER will accept the corresponding fact,
    so the system never invents information a platform never provided (section 12).
    Declaring nothing means "I can translate events"; it does not mean "I can tell you
    whether the agent used what it was given".

    The gating is enforced, not advisory: :class:`~aer.adapter.ingest.AdapterIngestor`
    refuses an adoption signal from an adapter that did not declare
    ``explicit_adoption_signal``. An honest ``UNKNOWN`` is worth more than a guessed
    ``ADOPTED``, because the second one becomes training data.
    """

    tool_events: bool = False
    """The integration reports tool calls and their results."""

    explicit_adoption_signal: bool = False
    """The platform can say whether the agent used a retrieved experience.

    Covers ignore/reject as well as adopt: they are the same kind of claim -- an
    observation about what the agent did with something it was offered.
    """

    human_feedback: bool = False
    """The platform carries a human's explicit signal (a thumb, an accept/reject)."""

    decision_summary: bool = False
    """The platform offers a public explanation of an action.

    The summary only -- never a private chain of thought, which AER refuses to store
    at all (section 6).
    """

    external_verification: bool = False
    """The platform can supply evidence for a real verifier to judge.

    Not the ability to declare a verdict: the verification engine still records it
    (section 39).
    """

    session_linkage: bool = False
    """The platform exposes a stable session identifier, so a reconnect can resume."""

    explicit_utility_signal: bool = False
    """The platform can say whether an experience *helped*, and not merely whether it
    was used.

    Separate from ``explicit_adoption_signal`` because they are different claims, and
    kept off by default so that an adapter never writes ``HELPFUL`` just because the
    task succeeded (sections 16 and 34).
    """

    context_injection: bool = False
    """The integration can put retrieved context into the model's actual request.

    This is the strongest claim in this class, so it is worth being precise about
    what it does and does not cover. It means the adapter has a path from AER's
    retrieval result into the text the model is next given -- not merely that AER
    retrieved something and held it in memory. A hook-based integration that can only
    observe a transcript cannot claim it, because the context it returns goes
    nowhere near the model.

    What it never covers is what the model *did* with the context: that remains
    ``explicit_adoption_signal``, and an integration may honestly declare this one
    while declaring adoption ``False`` (round-8.2 section 71).
    """

    def missing(self) -> tuple[str, ...]:
        """Names of the capabilities this adapter does **not** have.

        Used in error messages, so a refusal says which declaration is absent rather
        than only that something was refused.
        """
        return tuple(
            name
            for name in (
                "tool_events",
                "explicit_adoption_signal",
                "human_feedback",
                "decision_summary",
                "external_verification",
                "session_linkage",
                "explicit_utility_signal",
            )
            if not getattr(self, name)
        )


class AgentAction(BaseModel):
    """One normalised action an agent took (section 7).

    ``input_summary`` is a summary on purpose. The protocol's job is to carry enough
    for a human or a model to understand *what was attempted*, not to become a second
    blob store: the full payload of a large tool call belongs in an artifact, not in
    an event row (sections 7 and 27).
    """

    model_config = _FORBIDDEN_EXTRA

    kind: str
    """Adapter-defined category, e.g. ``"tool"``, ``"model"``, ``"shell"``, ``"api"``.

    Free-form because it describes the vendor's own taxonomy, and inventing a closed
    vocabulary for every agent's idea of an action would be AER deciding what a
    tool is for products it has never seen.
    """

    name: str
    tool_name: str | None = None
    input_summary: str | None = None


class AgentObservation(BaseModel):
    """One normalised thing that came back (section 8).

    Observations are the half of the loop an agent's own SDK records as a tool result;
    an adapter needs the same shape because a hook system reports them as separate
    deliveries, often late, and sometimes for tools AER never saw called.
    """

    model_config = _FORBIDDEN_EXTRA

    kind: ObservationKind
    summary: str | None = None
    detail: JsonObject = Field(default_factory=dict)


class AgentExecutionEnvelope(BaseModel):
    """One translated event, ready to be applied to a run (section 4).

    Produced by an adapter and consumed by
    :class:`~aer.adapter.ingest.AdapterIngestor`. Everything in it is either normalised
    AER vocabulary or an explicitly-external identifier; nothing in it is a vendor's
    shape (section 5).

    ``external_event_id`` is the one field the protocol cannot default. Without it, a
    retried delivery cannot be recognised as a retry, and the guarantee that a hook
    that fires twice produces one event is unenforceable (section 18). It is optional
    only because a platform may genuinely have no event identity -- in which case the
    adapter accepts that its integration cannot be made idempotent, which is a
    trade-off it should make knowingly.
    """

    model_config = _FORBIDDEN_EXTRA

    event_type: EventType = Field(
        description="AER vocabulary, not the vendor's. Unknown values never reach here."
    )
    identity: AgentIdentity

    protocol_version: str = AER_ADAPTER_PROTOCOL_VERSION

    external_event_id: str | None = None
    external_session_id: str | None = None
    external_run_id: str | None = None

    external_timestamp: AwareDatetime | None = None
    """The source's clock, kept as evidence and **never** as AER's ``created_at``.

    An Agent with a misconfigured clock must not be able to move events around in a
    timeline, so the authoritative timestamp of a run stays AER's own receive clock
    (section 24).
    """

    external_sequence: int | None = None
    """The source's ordering, preserved verbatim next to AER's arrival order.

    AER's ``sequence`` is never recomputed from this: a tool result that arrived after
    two unrelated events is the third event, and rewriting it to second would be
    editing history (section 23).
    """

    action: AgentAction | None = None
    observation: AgentObservation | None = None

    payload: JsonObject = Field(default_factory=dict)
    """Event-specific body, in the canonical shape documented per event type.

    Used when :attr:`action` / :attr:`observation` do not apply, and as the location of
    the typed fields those two do not carry (a failure's ``error_type``, a recovery's
    ``reason``, a feedback's ``approved``).
    """

    metadata: JsonObject = Field(default_factory=dict)
    """Free-form extras, sanitised before storage.

    This is where a ``decision_summary`` travels (section 6) -- and deliberately the
    only place a vendor's extra fields go, so the protocol's own fields cannot be
    overloaded with vendor meaning.
    """

    @property
    def body(self) -> JsonObject:
        """The event body: the generic payload, with the normalized form on top.

        Two sources, one rule. :attr:`payload` carries the event's canonical keys (a
        tool result's ``success``, a failure's ``error_type``, a recovery's ``reason``);
        :attr:`action` / :attr:`observation` carry the normalised shape. Merging means an
        adapter can fill both without either being thrown away, and the normalised form
        wins on a key collision because it is the form the protocol is defined in terms
        of. Returning one *instead of* the other would force a choice the protocol has
        no reason to force.
        """
        body: JsonObject = dict(self.payload)
        if self.action is not None:
            # ``exclude_defaults`` as well as ``exclude_none``: an observation with no
            # detail should not put ``"detail": {}`` into the trace, because an empty
            # object reads as "there was detail and it was empty".
            body.update(
                self.action.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            )
        if self.observation is not None:
            body.update(
                self.observation.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            )
        return body

    def require_ingestable(self) -> None:
        """Refuse an event type that only the lifecycle or the engine may write.

        Raises:
            UnsupportedAdapterEvent: the type is outside :data:`ADAPTER_EVENT_TYPES`.
                The message names the supported route, because an adapter author hitting
                this needs to know what to do instead, not only that they were wrong.
        """
        if self.event_type in ADAPTER_EVENT_TYPES:
            return
        if self.event_type.value in _RESPONSIBLY_CLOSED:
            guidance = {
                EventType.TASK_START.value: (
                    "a run is started by opening an adapter session (AdapterIngestor.open)"
                ),
                EventType.TASK_END.value: (
                    "a run is finished by closing an adapter session (AdapterIngestor.close)"
                ),
                EventType.VERIFICATION.value: (
                    "verdicts are written by the verification engine; relay the evidence "
                    "to a verifier through AdapterIngestor.verify instead"
                ),
            }[self.event_type.value]
            raise UnsupportedAdapterEvent(
                f"{self.event_type.value} is not a protocol event: {guidance}."
            )
        raise UnsupportedAdapterEvent(
            f"{self.event_type.value} is not a protocol event. Supported: "
            f"{sorted(item.value for item in ADAPTER_EVENT_TYPES)}."
        )


@dataclass(frozen=True, slots=True)
class AdapterSessionRequest:
    """What an adapter learned from a vendor's "a session is starting" payload.

    The adapter's job stops at producing this: deciding whether that means a new run,
    a resumed one, or a refusal is AER's, because it is the part that has to be
    consistent across every integration (section 9).
    """

    external_session_id: str
    task: str

    task_type: str | None = None
    external_run_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterFinishRequest:
    """What an adapter learned from a vendor's "the session ended" payload.

    The two "nothing was declared" answers are different and both are needed:

    * ``status=None`` -- the session is **not over** as far as AER is concerned. The run
      stays ``RUNNING``. Reach for this when the platform disconnected, or when the
      payload says nothing about an ending at all;
    * ``status=RunStatus.INCONCLUSIVE`` -- the session **is over** and nobody said how
      it went. The run closes, and its outcome is left open to verification rather than
      being guessed at.

    What no answer may be is an invented verdict: an adapter is not entitled to decide
    that a task succeeded, or that it failed, when the platform did not say so (sections
    10, 39; round-8.1.1 D-100).
    """

    status: RunStatus | None = None
    reason: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    """The three translations an integration implements (section 9).

    Implementations are deliberately small and stateless: an adapter that remembers
    something between calls is an adapter that loses it when the Agent process
    restarts, and everything worth remembering (the session mapping, the ledger) is
    persisted by AER instead (sections 21 and 51).

    An adapter **never** touches storage. It has no repository, no session and no
    engine: it turns vendor payloads into protocol values and returns them (section
    10). That is what keeps the terminal-state guard, the single error pipeline and the
    sequence allocation out of reach of a third-party implementation.
    """

    @property
    def name(self) -> str:
        """Stable adapter identifier, e.g. ``"aer-generic"``."""
        ...

    @property
    def protocol_version(self) -> str:
        """The protocol version this adapter was written against (section 36)."""
        ...

    def identity(self) -> AgentIdentity:
        """Who this adapter speaks for."""
        ...

    def capabilities(self) -> AdapterCapabilities:
        """What this integration can observe. Conservative by default (section 11)."""
        ...

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        """Translate a vendor's session-start payload.

        Raises:
            AdapterProtocolError: the payload is not a session start.
        """
        ...

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        """Translate one vendor event into zero or more protocol envelopes.

        Returning an empty sequence is a legitimate answer: a platform emits many
        events an evidence store has no business recording, and dropping them in the
        adapter is the right place for that decision.

        Every envelope derived from one vendor event must carry the **same**
        ``external_event_id``: the vendor's event is the unit of idempotency, so a
        translation that splits one delivery into two envelopes is still one event
        (section 18).
        """
        ...

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        """Translate a vendor's session-end payload."""
        ...
