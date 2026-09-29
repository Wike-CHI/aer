"""The AER side of the DSH adapter: protocol translation and the runtime sink.

This module is what makes the integration an *AER* adapter rather than a recorder.
:mod:`aer.adapter.dsh.bridge` can run against a sink that writes a JSONL file and
proves the transport; this module supplies the sink that actually opens runs, applies
events, retrieves experience and records that the retrieved context reached the model.

Run granularity (round-8.2 sections 11-13)
------------------------------------------

**One DSH turn is one AER run.** DSH already publishes ``turn/start`` and ``turn/end``,
so a turn arrives with a definite beginning, a definite ending and a stated reason for
that ending -- which is much closer to "one attempt at a task" than a session is. A
session may hold several unrelated tasks and would force AER to describe them with one
task text.

The consequence is the part that matters: one external session maps to *many* runs.
AER's adapter layer already expresses that with :meth:`AdapterIngestor.reopen`, which
starts a new run for an external session whose previous run finished and keeps the
earlier run ids on the mapping rather than overwriting them. Nothing about DSH's
semantics had to be bent to fit.

Provider versus harness (section 55)
------------------------------------

DSH is a harness, not a model provider: the same installation routes to DeepSeek, to
an OpenAI-compatible endpoint, or to anything else a deployment configures. So
``agent_name`` is ``"dsh"`` while ``provider`` is whatever the runtime actually
resolved, read from the durable ``request/header`` event. It is never hard-coded, and
a deployment that changes provider changes the recorded identity without a code edit.

What this adapter never claims (sections 31, 33, 71)
----------------------------------------------------

It can put text into the model's request, so it declares ``context_injection``. It
cannot say whether the model *used* that text, so it declares adoption ``False`` and
leaves the usage signal ``UNKNOWN``. It cannot say whether the text *helped*, so it
declares utility ``False``. And it never turns a successful turn into ``HELPFUL``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Literal, overload

from aer.adapter.dsh.protocol import BridgeRequest
from aer.adapter.protocol import (
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterSessionRequest,
    AgentAction,
    AgentExecutionEnvelope,
    AgentIdentity,
    AgentObservation,
    ObservationKind,
)
from aer.exceptions import AdapterProtocolError
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.serialization import JsonObject, to_json_object

__all__ = [
    "ADAPTER_NAME",
    "DSH_TURN_OUTCOMES",
    "DshAdapter",
    "RuntimeSink",
    "dsh_capabilities",
]

#: The adapter's stable name. Also the ``source.plugin`` value DSH sees.
ADAPTER_NAME: Final = "aer-dsh"

#: The agent this adapter speaks for. The harness, not a model provider (section 55).
AGENT_NAME: Final = "dsh"

#: The provider recorded when the runtime has not told us which one it resolved.
#:
#: Deliberately not ``"deepseek"``: guessing a provider is exactly the confusion
#: section 55 warns about, and an unknown route must be visible as unknown.
UNKNOWN_PROVIDER: Final = "unknown"

#: The AER status each DSH turn outcome justifies.
#:
#: Only ``error`` and ``aborted`` are declarations about the work. ``completed`` means
#: the *loop* stopped normally, which is not the task succeeding (section 16), and the
#: rest say nothing at all. Every non-declaration therefore maps to ``INCONCLUSIVE``,
#: which is the status that claims nothing and stays open to verification (D-100).
DSH_TURN_OUTCOMES: Final[Mapping[str, RunStatus]] = {
    "completed": RunStatus.INCONCLUSIVE,
    "aborted": RunStatus.ABORTED,
    "blocked": RunStatus.INCONCLUSIVE,
    "error": RunStatus.FAILED,
    "max-tokens": RunStatus.INCONCLUSIVE,
    "interrupted": RunStatus.INCONCLUSIVE,
}

#: Envelope types this adapter will translate, mirroring the TypeScript side.
_TRANSLATED: Final[frozenset[str]] = frozenset(
    {
        EventType.MODEL_CALL.value,
        EventType.MODEL_RESULT.value,
        EventType.TOOL_CALL.value,
        EventType.TOOL_RESULT.value,
        EventType.ERROR.value,
    }
)


def dsh_capabilities() -> AdapterCapabilities:
    """What the DSH integration can actually observe (section 53).

    ``tool_events`` because the durable log carries structured calls and results with
    a correlation id and an explicit ``isError``; ``session_linkage`` because DSH
    sessions have stable ids and a monotonic seq, which is what makes replay possible;
    ``context_injection`` because ``agent/pre-step`` can add a message to the very
    request the model is about to see.

    Everything else stays off. DSH gives no adoption signal, no utility signal, no
    human feedback and no verdict, and claiming any of them would turn an absence of
    evidence into evidence.
    """
    return AdapterCapabilities(
        tool_events=True,
        explicit_adoption_signal=False,
        human_feedback=False,
        decision_summary=False,
        external_verification=False,
        session_linkage=True,
        explicit_utility_signal=False,
        context_injection=True,
    )


@dataclass(frozen=True, slots=True)
class DshAdapter:
    """Translation between DSH's normalized envelope and the AER protocol.

    Stateless by design, like every adapter: the session mapping, the ledger and the
    cursor are AER's to persist, so an adapter that remembered anything would lose it
    when the agent process restarts.

    ``provider`` and ``model`` are per-route facts read from the runtime, so a session
    that changes model gets a different identity rather than a wrong one.
    :meth:`for_route` is how a sink obtains a correctly-identified instance.
    """

    provider: str = UNKNOWN_PROVIDER
    model: str | None = None
    agent_version: str | None = None
    adapter_version: str = "0.1.0"

    @property
    def name(self) -> str:
        """Stable adapter identifier."""
        return ADAPTER_NAME

    @property
    def protocol_version(self) -> str:
        """The protocol version this adapter was written against."""
        from aer.adapter.protocol import AER_ADAPTER_PROTOCOL_VERSION

        return AER_ADAPTER_PROTOCOL_VERSION

    def for_route(self, *, provider: str | None, model: str | None) -> DshAdapter:
        """This adapter, identified for one resolved provider route.

        Returns ``self`` unchanged when the route carries nothing new, so a caller can
        apply it unconditionally without churning identity objects.
        """
        if provider is None and model is None:
            return self
        return replace(
            self,
            provider=provider if provider else self.provider,
            model=model if model is not None else self.model,
        )

    def identity(self) -> AgentIdentity:
        """Who this adapter speaks for."""
        return AgentIdentity(
            provider=self.provider,
            agent_name=AGENT_NAME,
            adapter_name=self.name,
            adapter_version=self.adapter_version,
            agent_version=self.agent_version,
            model=self.model,
        )

    def capabilities(self) -> AdapterCapabilities:
        """What this integration can observe."""
        return dsh_capabilities()

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        """Translate a DSH turn into a session-opening request.

        The task text is the turn's own user text, already extracted on the TypeScript
        side from the durable ``user/message`` events that belong to the turn, so a
        queued or injected message cannot masquerade as the task.
        """
        session = _mapping(raw, "session")
        external_session_id = _text(session, "external_session_id", required=True)
        task = _text(raw, "task") or ""
        if task == "":
            # A turn with no user text still opened a run; describing it as empty
            # would make the trace unreadable, and inventing a task would be worse.
            task = f"(DSH turn {_turn_label(raw)}: no user text in the durable log)"
        metadata: dict[str, object] = {"dsh_turn": _turn_label(raw)}
        model = _nested(raw, "model")
        if model is not None:
            metadata["dsh_route"] = model
        return AdapterSessionRequest(
            external_session_id=external_session_id,
            task=task,
            metadata=metadata,
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        """Translate one normalized DSH envelope.

        The TypeScript plugin has already reduced the durable event to AER's
        vocabulary, so the translation here is validation plus typing -- not a second
        mapping. Anything the plugin labelled with an event type this adapter does not
        translate is dropped rather than coerced.
        """
        event_type = _text(raw, "event_type", required=True)
        if event_type not in _TRANSLATED:
            return ()
        envelope = AgentExecutionEnvelope(
            event_type=EventType(event_type),
            identity=self.identity(),
            external_event_id=_text(raw, "external_event_id"),
            external_session_id=_text(raw, "external_session_id"),
            external_sequence=_sequence(raw),
            external_timestamp=_timestamp(raw),
            action=self._action(raw),
            observation=self._observation(raw),
            payload=_payload(raw),
            metadata=_metadata(raw),
        )
        return (envelope,)

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        """Translate a DSH ``turn/end`` into a run-closing request.

        An unrecognized reason closes the run as ``INCONCLUSIVE``: DSH's
        ``TurnEndReasonMap`` is merge-extensible, and leaving a run ``RUNNING`` forever
        because a newer harness added a variant would be worse than closing it without
        a claim.
        """
        reason_kind = _text(raw, "reason_kind") or "unknown"
        status = DSH_TURN_OUTCOMES.get(reason_kind, RunStatus.INCONCLUSIVE)
        return AdapterFinishRequest(
            status=status,
            reason=reason_kind,
            metadata={"dsh_turn_end": to_json_object(dict(_nested(raw, "detail") or {}))},
        )

    # -- normalized halves -------------------------------------------------

    def _action(self, raw: Mapping[str, object]) -> AgentAction | None:
        """The action half, for the events that have one."""
        payload = _payload(raw)
        event_type = _text(raw, "event_type", required=True)
        if event_type in (EventType.TOOL_CALL.value, EventType.TOOL_RESULT.value):
            name = _text(payload, "tool") or _text(payload, "call_id") or "tool"
            return AgentAction(
                kind="tool",
                name=name,
                tool_name=name,
                input_summary=_optional_text(payload.get("arguments")),
            )
        if event_type in (EventType.MODEL_CALL.value, EventType.MODEL_RESULT.value):
            return AgentAction(kind="model", name="model")
        return None

    def _observation(self, raw: Mapping[str, object]) -> AgentObservation | None:
        """The observation half: what came back, when anything did.

        ``isError`` is DSH's own structured outcome, so the success or failure recorded
        here is a stated fact and not an inference from the result's text (section 19).
        """
        payload = _payload(raw)
        event_type = _text(raw, "event_type", required=True)
        if event_type != EventType.TOOL_RESULT.value:
            return None
        failed = payload.get("success") is False
        detail: dict[str, object] = {}
        call_id = payload.get("call_id")
        if isinstance(call_id, str):
            detail["call_id"] = call_id
        error = payload.get("error")
        if isinstance(error, Mapping):
            error_type = error.get("error_type")
            if isinstance(error_type, str):
                detail["error_type"] = error_type
        return AgentObservation(
            kind=ObservationKind.TOOL_FAILURE if failed else ObservationKind.TOOL_SUCCESS,
            summary=_optional_text(payload.get("result")),
            detail=to_json_object(detail),
        )


@dataclass
class RuntimeSink:
    """The bridge sink that drives a real AER runtime.

    One instance per bridge process, holding the live binding between DSH turns and
    AER runs. It owns nothing durable: the ingestor persists the session mapping and
    the event ledger, so a crash here loses at most the in-memory handle, and the next
    turn -- or a replay from DSH's durable log -- reopens it.

    Retrieval is the one operation on the model's critical path. It is bounded by the
    plugin's own timeout and, if it fails, the turn proceeds without experience while
    the retrieval itself has already been recorded (section 51). An injection is
    recorded only when DSH confirms the message is in the durable log, which is what
    makes "retrieved but never injected" a state AER can distinguish (sections 29, 72).
    """

    ingestor: Any
    adapter: DshAdapter = field(default_factory=DshAdapter)
    #: Provider/model last resolved from `request/header`, per session id.
    routes: dict[str, DshAdapter] = field(default_factory=dict)
    #: The open handle per external session id. One at a time: turns are sequential.
    handles: dict[str, Any] = field(default_factory=dict)
    #: Sessions that have already produced a run, whether or not one is open now.
    #:
    #: Separate from :attr:`handles` because a turn's handle is released when the turn
    #: ends, and "this session has no open turn" is a different fact from "this session
    #: has never had a run". Without it the second turn would call ``open()`` again and
    #: be refused as a terminated session instead of reopening as a new episode.
    opened: set[str] = field(default_factory=set)
    #: The retrieval result awaiting injection confirmation, per session id.
    pending: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    # -- BridgeSink --------------------------------------------------------

    def observe(self, request: BridgeRequest, result: Mapping[str, Any] | None) -> None:
        """Count deliveries, so a probe can tell talking from delivering."""
        self.counts[request.operation] = self.counts.get(request.operation, 0) + 1

    def session_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Record that a DSH session exists. A run opens with its first turn."""
        session = payload.get("session")
        if isinstance(session, Mapping):
            sid = session.get("external_session_id")
            if isinstance(sid, str):
                self.routes.setdefault(sid, self.adapter)
        return {"accepted": True}

    def turn_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Open the AER run for one DSH turn."""
        session = _mapping(payload, "session")
        sid = _text(session, "external_session_id", required=True)
        adapter = self._adapter_for(sid, payload)
        raw = {
            "session": dict(session),
            "task": payload.get("task"),
            "turn": payload.get("turn"),
            "model": payload.get("model"),
        }
        if sid not in self.opened:
            handle = self.ingestor.open(adapter, raw)
            self.opened.add(sid)
        else:
            # A later turn in one session: an explicit new episode, not a resume of
            # the previous turn's run (section 13).
            handle = self.ingestor.reopen(adapter, raw)
        self.handles[sid] = handle
        return {"run_id": handle.run_id, "outcome": handle.outcome.value}

    def event_ingest(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Apply one normalized envelope to the turn's run."""
        envelope = payload.get("envelope")
        if not isinstance(envelope, Mapping):
            raise AdapterProtocolError("event.ingest requires an 'envelope' object")
        sid = _text(envelope, "external_session_id") or ""
        handle = self.handles.get(sid)
        if handle is None:
            # No open turn: the event cannot be attributed, and guessing which run it
            # belongs to would be manufacturing evidence.
            return {"applied": False, "reason": "no open turn for this session"}
        result = self.ingestor.ingest(handle, dict(envelope))
        return {
            "applied": result.applied,
            "outcome": result.outcome.value,
            "run_id": result.run_id,
        }

    def turn_end(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Close the run for a finished turn, if DSH stated how it ended."""
        session = _mapping(payload, "session")
        sid = _text(session, "external_session_id", required=True)
        handle = self.handles.pop(sid, None)
        if handle is None:
            return {"closed": False, "reason": "no open turn for this session"}
        raw = {
            "session": dict(session),
            "turn": payload.get("turn"),
            "reason_kind": payload.get("reason_kind"),
            "detail": payload.get("detail") or {},
        }
        run = self.ingestor.close(handle, raw)
        self.pending.pop(sid, None)
        return {
            "closed": run is not None,
            "run_id": handle.run_id,
            "status": None if run is None else run.status.value,
        }

    def retrieve(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Retrieve experience for this turn and render it for the model.

        The retrieved context is returned to the plugin, which is the only component
        that can put it in front of the model. Nothing is recorded as injected here:
        that happens in :meth:`injected`, once DSH's durable log proves the text
        actually entered a request (section 70).
        """
        session = _mapping(payload, "session")
        sid = _text(session, "external_session_id", required=True)
        handle = self.handles.get(sid)
        if handle is None:
            return {"retrieval_id": "none", "context_text": "", "experience_count": 0}
        task = _text(payload, "task") or ""
        tracked = self.ingestor.retrieve(handle, task)
        hits = list(tracked.result.all_hits)
        if not hits:
            return {
                "retrieval_id": tracked.session_id,
                "context_text": "",
                "experience_count": 0,
            }
        formatter = _formatter()
        text = formatter.format(tracked.result)
        self.pending[sid] = tracked
        return {
            "retrieval_id": tracked.session_id,
            "context_text": text,
            "experience_count": len(hits),
            "formatter_version": _formatter_version(),
        }

    def injected(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Record that retrieved context entered the model's request.

        Called only after the plugin has seen the context it returned come back as a
        durable ``user/message``, so this is a fact rather than an intention.
        """
        session = _mapping(payload, "session")
        sid = _text(session, "external_session_id", required=True)
        handle = self.handles.get(sid)
        tracked = self.pending.pop(sid, None)
        if handle is None or tracked is None:
            return {"recorded": False, "reason": "no pending retrieval for this session"}
        usage = self.ingestor.inject(handle, tracked)
        return {"recorded": True, "injected": len(usage)}

    def close(self) -> None:
        """Release in-memory state. Nothing durable is owned here."""
        self.handles.clear()
        self.pending.clear()

    # -- route identity ----------------------------------------------------

    def _adapter_for(self, sid: str, payload: Mapping[str, Any]) -> DshAdapter:
        """The adapter identified for this session's resolved route."""
        model = payload.get("model")
        provider = None
        if isinstance(model, Mapping):
            provider = model.get("provider") if isinstance(model.get("provider"), str) else None
            model_id = model.get("model") if isinstance(model.get("model"), str) else None
        else:
            model_id = None
        base = self.routes.get(sid, self.adapter)
        if provider is None and model_id is None:
            return base
        resolved = base.for_route(provider=provider, model=model_id)
        self.routes[sid] = resolved
        return resolved


def _formatter() -> Any:
    """The renderer used for model-facing experience context."""
    from aer.knowledge.formatter import ExperienceContextFormatter

    return ExperienceContextFormatter()


def _formatter_version() -> str:
    """Which renderer produced the text."""
    from aer.knowledge.formatter import FORMATTER_VERSION

    return FORMATTER_VERSION


# -- payload helpers -------------------------------------------------------


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One nested object, required."""
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise AdapterProtocolError(f"{key} must be an object")
    return value


def _nested(raw: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """One nested object, optional."""
    value = raw.get(key)
    return value if isinstance(value, Mapping) else None


@overload
def _text(raw: Mapping[str, Any], key: str, *, required: Literal[True]) -> str: ...


@overload
def _text(raw: Mapping[str, Any], key: str, *, required: bool = False) -> str | None: ...


def _text(raw: Mapping[str, Any], key: str, *, required: bool = False) -> str | None:
    """One string field.

    Two overloads rather than a ``cast`` at every call site: a required field that
    arrives absent is a protocol disagreement between the two halves of this adapter,
    and the callers read as if the value were simply there.
    """
    value = raw.get(key)
    if isinstance(value, str) and value != "":
        return value
    if required:
        raise AdapterProtocolError(f"{key} is required")
    return None


def _optional_text(value: object) -> str | None:
    """A string field that may legitimately be absent."""
    return value if isinstance(value, str) and value != "" else None


def _sequence(raw: Mapping[str, Any]) -> int | None:
    """DSH's own ordering, preserved beside AER's arrival order (section 23)."""
    value = raw.get("external_sequence")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(raw: Mapping[str, Any]) -> datetime | None:
    """The source's clock, as evidence only -- never as AER's ``created_at``."""
    value = raw.get("external_timestamp")
    if not isinstance(value, str) or value == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AdapterProtocolError(f"external_timestamp is not ISO-8601: {value!r}") from exc


def _payload(raw: Mapping[str, Any]) -> JsonObject:
    """The envelope's canonical body."""
    value = raw.get("payload")
    return to_json_object(dict(value)) if isinstance(value, Mapping) else {}


def _metadata(raw: Mapping[str, Any]) -> JsonObject:
    """Vendor extras, isolated from the protocol's own fields."""
    value = raw.get("metadata")
    return to_json_object(dict(value)) if isinstance(value, Mapping) else {}


def _turn_label(raw: Mapping[str, Any]) -> str:
    """The turn number, for metadata and for an empty task's description."""
    turn = raw.get("turn")
    return str(turn) if isinstance(turn, (int, str)) else "?"
