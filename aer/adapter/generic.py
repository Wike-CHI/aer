"""The reference adapter: standard JSON in, protocol envelopes out.

This exists to prove one thing (round-8 brief, sections 30-31): that the protocol is
*sufficient*. Everything AER can record -- runs, traces, retrieval, usage, verification
-- can be driven through it by an adapter that knows nothing about AER's internals and
holds no state. If a capability is missing from the protocol, this adapter cannot be
written, and that is a much cheaper discovery than a vendor's integration being the
thing that finds out.

It is deliberately **not** a production adapter. Real integrations have to cope with a
vendor's actual payloads, version changes, partial deliveries and quirks, and each of
those belongs to its own milestone (M8.x). What the generic adapter is for:

* a working example for whoever writes the next adapter;
* the thing the test suite drives two *different* fake agents through, to show that
  two unrelated vocabularies produce identical AER semantics (section 60);
* a way to exercise the whole pipeline -- run, trace, retrieval, usage, verification --
  without any vendor in the loop.

Its input is the shape section 30 names::

    {"type": "tool_call", "tool": "shell", "input": "pytest -q"}
    {"type": "tool_result", "tool": "shell", "success": false,
     "error": {"error_type": "shell.NonZeroExit", "message": "2 failed"}}
    {"type": "error", "error_type": "builtins.TimeoutError", "message": "timed out"}
    {"type": "recovery_start", "reason": "grant the capability", "error_id": "..."}
    {"type": "recovery_result", "success": true}

Common fields on any event: ``event_id``, ``sequence``, ``timestamp``, ``session_id``,
``decision_summary``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from aer.adapter.protocol import (
    AER_ADAPTER_PROTOCOL_VERSION,
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterSessionRequest,
    AgentAction,
    AgentExecutionEnvelope,
    AgentIdentity,
    AgentObservation,
    ObservationKind,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.serialization import JsonObject, to_json_object

__all__ = ["GenericAgentAdapter"]

#: Vendor event types this adapter translates, keyed by the ``type`` field.
_EVENT_TYPES: Mapping[str, EventType] = {
    "tool_call": EventType.TOOL_CALL,
    "tool_result": EventType.TOOL_RESULT,
    "model_call": EventType.MODEL_CALL,
    "model_result": EventType.MODEL_RESULT,
    "error": EventType.ERROR,
    "recovery_start": EventType.RECOVERY_START,
    "recovery_result": EventType.RECOVERY_RESULT,
    "human_feedback": EventType.HUMAN_FEEDBACK,
    # Mapped rather than rejected so the refusal comes from the protocol, which can say
    # what to do instead: relay the evidence to a verifier (section 39).
    "verification": EventType.VERIFICATION,
}

#: Vendor event types that are known and deliberately carry no evidence.
#:
#: A real hook stream is mostly heartbeats and progress lines. Dropping them in the
#: adapter is the right place for that decision, and naming them here keeps the
#: distinction between "we chose not to record this" and "we do not know what this is".
_IGNORED_TYPES: frozenset[str] = frozenset(
    {"heartbeat", "ping", "telemetry", "log", "progress", "session_note"}
)

_STATUS_WORDS: Mapping[str, RunStatus] = {
    "success": RunStatus.SUCCESS,
    "succeeded": RunStatus.SUCCESS,
    "completed": RunStatus.SUCCESS,
    "done": RunStatus.SUCCESS,
    "partial_success": RunStatus.PARTIAL_SUCCESS,
    "partial": RunStatus.PARTIAL_SUCCESS,
    "failed": RunStatus.FAILED,
    "failure": RunStatus.FAILED,
    "error": RunStatus.FAILED,
    "aborted": RunStatus.ABORTED,
    "cancelled": RunStatus.ABORTED,
    "canceled": RunStatus.ABORTED,
    # A platform may say the session is over without saying how it went. That is a
    # terminal answer, not a missing one, and spelling it is how an integration reports
    # "provider outcome UNKNOWN" without inventing a verdict (round-8.1.1, D-100).
    "unknown": RunStatus.INCONCLUSIVE,
    "inconclusive": RunStatus.INCONCLUSIVE,
}


class GenericAgentAdapter:
    """A vendor-agnostic adapter over a small, documented JSON vocabulary."""

    def __init__(
        self,
        *,
        provider: str = "generic",
        agent_name: str = "generic-agent",
        agent_version: str | None = None,
        model: str | None = None,
        model_version: str | None = None,
        adapter_name: str = "aer-generic",
        adapter_version: str = "1",
        capabilities: AdapterCapabilities | None = None,
    ) -> None:
        self._provider = provider
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._model = model
        self._model_version = model_version
        self._adapter_name = adapter_name
        self._adapter_version = adapter_version
        # Conservative by default: this adapter demonstrably translates tool events and
        # claims nothing else. Adoption, feedback and utility have to be switched on
        # deliberately by whoever knows their platform provides them (section 12).
        self._capabilities = capabilities or AdapterCapabilities(tool_events=True)

    # -- AgentAdapter ------------------------------------------------------

    @property
    def name(self) -> str:
        """Stable adapter identifier."""
        return self._adapter_name

    @property
    def protocol_version(self) -> str:
        """The protocol version this adapter was written against."""
        return AER_ADAPTER_PROTOCOL_VERSION

    def identity(self) -> AgentIdentity:
        """Who this adapter speaks for."""
        return AgentIdentity(
            provider=self._provider,
            agent_name=self._agent_name,
            agent_version=self._agent_version,
            model=self._model,
            model_version=self._model_version,
            adapter_name=self._adapter_name,
            adapter_version=self._adapter_version,
        )

    def capabilities(self) -> AdapterCapabilities:
        """What this integration declares it can observe."""
        return self._capabilities

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        """Translate ``{"session_id": ..., "task": ...}`` into a session request.

        Raises:
            AdapterProtocolError: the payload carries no task. A run whose description
                is empty cannot be retrieved for, distilled or understood, and inventing
                one would put a sentence in the trace that nobody wrote.
        """
        session_id = _text(raw, "session_id", "external_session_id")
        if session_id is None:
            raise AdapterProtocolError(
                "A generic session start must carry 'session_id'; without it a "
                "reconnect cannot find the run it was reporting into"
            )
        task = _text(raw, "task", "task_description")
        if task is None:
            raise AdapterProtocolError(
                "A generic session start must carry 'task'; the run would have nothing to describe"
            )
        return AdapterSessionRequest(
            external_session_id=session_id,
            task=task,
            task_type=_text(raw, "task_type"),
            external_run_id=_text(raw, "run_id", "external_run_id"),
            metadata=_extras(
                raw,
                exclude={
                    "session_id",
                    "external_session_id",
                    "task",
                    "task_description",
                    "task_type",
                    "run_id",
                    "external_run_id",
                },
            ),
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        """Translate one standard JSON event.

        Returns an empty sequence for a vendor event this adapter deliberately ignores,
        and raises for one it does not recognize: "we chose not to record this" and
        "we have never seen this" are different answers, and only one of them means the
        integration needs attention (section 41).

        Raises:
            UnsupportedAdapterEvent: the ``type`` is neither translated nor ignored.
            AdapterProtocolError: the payload is malformed for its type.
        """
        kind = _text(raw, "type", "event")
        if kind is None:
            raise AdapterProtocolError(
                "Every generic event must carry a 'type' field naming what it is"
            )
        if kind in _IGNORED_TYPES:
            return ()

        try:
            event_type = _EVENT_TYPES[kind]
        except KeyError as exc:
            raise UnsupportedAdapterEvent(
                f"The generic adapter does not recognize event type {kind!r}. It "
                f"translates {sorted(_EVENT_TYPES)} and ignores {sorted(_IGNORED_TYPES)}; "
                "a real adapter declares its own vocabulary here."
            ) from exc

        envelope = AgentExecutionEnvelope(
            event_type=event_type,
            identity=self.identity(),
            protocol_version=AER_ADAPTER_PROTOCOL_VERSION,
            external_event_id=_text(raw, "event_id", "external_event_id"),
            external_session_id=_text(raw, "session_id", "external_session_id"),
            external_run_id=_text(raw, "run_id", "external_run_id"),
            external_timestamp=_timestamp(raw.get("timestamp")),
            external_sequence=_integer(raw.get("sequence")),
            action=self._action(kind, raw),
            observation=self._observation(kind, raw),
            payload=self._payload(kind, raw),
            metadata=self._metadata(raw),
        )
        return (envelope,)

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        """Translate ``{"status": "success"}`` into a finish request.

        An unrecognized or absent status leaves the run ``RUNNING`` rather than guessing:
        a platform that says "wrapped up" must not have a verdict written on its behalf.
        A platform that *has* ended the session but has no outcome should say so --
        ``{"status": "unknown"}`` -- which closes the run ``INCONCLUSIVE`` and still
        leaves it open to being settled by verification.
        """
        status = _status(raw.get("status"))
        return AdapterFinishRequest(
            status=status,
            reason=_text(raw, "reason", "summary"),
            metadata=_extras(raw, exclude={"status", "reason", "summary"}),
        )

    # -- per-event translation ---------------------------------------------

    def _action(self, kind: str, raw: Mapping[str, object]) -> AgentAction | None:
        """The normalised action for the events that describe one."""
        if kind == "tool_call":
            tool = _text(raw, "tool", "tool_name", "name")
            return AgentAction(
                kind="tool",
                name=tool or "unnamed-tool",
                tool_name=tool,
                input_summary=_text(raw, "input_summary", "input", "arguments"),
            )
        if kind == "model_call":
            return AgentAction(
                kind="model",
                name=_text(raw, "model") or self._model or "model",
                input_summary=_text(raw, "input_summary", "prompt_summary"),
            )
        return None

    def _observation(self, kind: str, raw: Mapping[str, object]) -> AgentObservation | None:
        """The normalised observation for the events that describe one."""
        if kind == "tool_result":
            success = raw.get("success")
            failed = success is False
            return AgentObservation(
                kind=ObservationKind.TOOL_FAILURE if failed else ObservationKind.TOOL_SUCCESS,
                summary=_text(raw, "summary") or _error_message(raw.get("error")),
                detail={"tool": _text(raw, "tool", "tool_name")}
                if _text(raw, "tool", "tool_name")
                else {},
            )
        if kind == "model_result":
            return AgentObservation(
                kind=ObservationKind.ENVIRONMENT,
                summary=_text(raw, "output_summary", "summary"),
            )
        if kind == "human_feedback":
            return AgentObservation(
                kind=ObservationKind.HUMAN,
                summary=_text(raw, "comment", "summary"),
            )
        return None

    def _payload(self, kind: str, raw: Mapping[str, object]) -> JsonObject:
        """The canonical keys the ingestor reads, preserved verbatim.

        The generic adapter emits both the normalised shape and these keys, because the
        ingestor's dispatch is defined in terms of them: a tool result's ``success``
        decides whether an error record is written, and a recovery's ``reason`` is the
        only thing that makes the attempt interpretable.
        """
        if kind == "tool_call":
            return _compact(
                {
                    "tool": _text(raw, "tool", "tool_name", "name"),
                    "arguments": raw.get("input", raw.get("arguments")),
                }
            )
        if kind == "tool_result":
            success = raw.get("success")
            if not isinstance(success, bool):
                raise AdapterProtocolError(
                    "A tool_result must state success as a boolean. It is the field "
                    "that decides whether an ErrorRecord is written, and guessing it "
                    "would put a failure -- or a success -- into the trace that the "
                    "source never reported."
                )
            return _compact(
                {
                    "tool": _text(raw, "tool", "tool_name"),
                    "success": success,
                    "result": raw.get("result", raw.get("output")),
                    "error": _compact(
                        {
                            "error_type": _text(raw.get("error"), "error_type")
                            if isinstance(raw.get("error"), Mapping)
                            else None,
                            "message": _error_message(raw.get("error")),
                            "stack_trace": _text(raw.get("error"), "stack_trace")
                            if isinstance(raw.get("error"), Mapping)
                            else None,
                        }
                    ),
                }
            )
        if kind == "error":
            return _compact(
                {
                    "error_type": _text(raw, "error_type", "type_name"),
                    # No default message: if the source described nothing, saying so is
                    # the ingestor's job, and it says it in one place.
                    "message": _text(raw, "message", "error"),
                    "stack_trace": _text(raw, "stack_trace", "stack"),
                    "recoverable": raw.get("recoverable"),
                }
            )
        if kind == "recovery_start":
            return _compact(
                {
                    "reason": _text(raw, "reason", "summary"),
                    "error_id": _text(raw, "error_id"),
                }
            )
        if kind == "recovery_result":
            return _compact(
                {
                    "success": raw.get("success"),
                    "result": raw.get("result"),
                    "recovery_id": _text(raw, "recovery_id"),
                    "duration_ms": _integer(raw.get("duration_ms")),
                }
            )
        if kind == "human_feedback":
            return _compact(
                {
                    "approved": raw.get("approved"),
                    "comment": _text(raw, "comment"),
                    "reviewer": _text(raw, "reviewer"),
                }
            )
        if kind in ("model_call", "model_result"):
            return _compact(
                {
                    "model": _text(raw, "model"),
                    "summary": _text(raw, "summary", "prompt_summary", "output_summary"),
                }
            )
        if kind == "verification":
            return _compact(
                {
                    "verifier": _text(raw, "verifier"),
                    "passed": raw.get("passed"),
                }
            )
        return {}

    def _metadata(self, raw: Mapping[str, object]) -> JsonObject:
        """Everything else the caller attached, including any decision summary.

        Not filtered here: the protocol's job is to carry what the source said, and AER's
        sanitizer decides what may be stored (it drops private reasoning and redacts
        credentials). An adapter that filtered on its own would hide the fact that the
        platform had sent something AER refuses to keep.
        """
        reserved = {
            "type",
            "event",
            "event_id",
            "external_event_id",
            "session_id",
            "external_session_id",
            "run_id",
            "external_run_id",
            "timestamp",
            "sequence",
            "tool",
            "tool_name",
            "name",
            "input",
            "arguments",
            "input_summary",
            "prompt_summary",
            "output_summary",
            "result",
            "output",
            "success",
            "error",
            "error_type",
            "type_name",
            "message",
            "stack",
            "stack_trace",
            "recoverable",
            "reason",
            "summary",
            "comment",
            "reviewer",
            "approved",
            "status",
            "duration_ms",
            "model",
        }
        return _extras(raw, exclude=reserved)


# ---------------------------------------------------------------------------
# input helpers
# ---------------------------------------------------------------------------


def _text(source: object, *keys: str) -> str | None:
    """First non-empty string among ``keys``, looked up in ``source`` when it is a map.

    Accepts several spellings because vendors differ on ``session_id`` versus
    ``sessionId`` versus ``external_session_id``, and the adapter is exactly the place
    where that difference is supposed to disappear.
    """
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp with a timezone, or ``None``.

    A naive timestamp is dropped rather than assumed to be local: an Agent in another
    timezone would otherwise have its events silently shifted (section 24).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _status(value: object) -> RunStatus | None:
    """Translate a vendor's status word, or ``None`` when it is not one we know."""
    if isinstance(value, RunStatus):
        return value
    if isinstance(value, str):
        return _STATUS_WORDS.get(value.strip().casefold())
    return None


def _error_message(error: object) -> str | None:
    """A human-readable message from an ``error`` that may be a map or a string."""
    if isinstance(error, str) and error.strip():
        return error
    if isinstance(error, Mapping):
        for key in ("message", "summary", "detail"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _extras(raw: Mapping[str, object], *, exclude: set[str]) -> JsonObject:
    """Everything in ``raw`` that the adapter did not consume itself."""
    return to_json_object({key: value for key, value in raw.items() if key not in exclude})


def _compact(mapping: Mapping[str, object]) -> JsonObject:
    """Drop the ``None`` entries so a payload states only what was actually known."""
    return to_json_object({key: value for key, value in mapping.items() if value is not None})
