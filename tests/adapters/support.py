"""Two fake Agents with nothing in common, and the adapters that make them equal.

The acceptance criterion for this milestone is a sentence about *two* integrations
(round-8 brief, section 60)::

    two completely different Agent implementations, going through the same Adapter
    Protocol, produce the same AER evidence without AER Core knowing anything about
    either of them.

So the fakes here are deliberately not variations on one theme. One of them is a shell
runner whose whole vocabulary is commands and exit codes; the other is a structured
tool API whose vocabulary is phases and action envelopes. They disagree about what an
event is called, what a result looks like, how a failure is described, and how a
session ends. If the protocol were only sufficient for one shape of vendor, the second
fake could not be written -- and that would be the finding.

Both drive real raw payloads through a real adapter and a real ingestor. Nothing here
reaches around the protocol to make a scenario easier: a fake that called
``runtime.start_run`` directly would prove nothing about an integration's ability to do
the same thing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from aer import AER, AdapterSessionHandle, TrackedRetrievalResult
from aer.adapter import (
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterIngestor,
    AdapterSessionRequest,
    AgentAction,
    AgentExecutionEnvelope,
    AgentIdentity,
    GenericAgentAdapter,
    IngestResult,
)
from aer.exceptions import AdapterError, UnsupportedAdapterEvent
from aer.runtime.enums import AdapterIngestOutcome, EventType, RunStatus, UsageSignal

DEFAULT_TASK = "fix the REST API 403 on the product page"
DEFAULT_QUERY = "WordPress REST API 403"


# ---------------------------------------------------------------------------
# a shell-style agent
# ---------------------------------------------------------------------------


class ShellStyleAdapter:
    """Translates a command-runner vocabulary: ``exec``, ``stdout``, ``exit_code``.

    A hook system that only ever sees a process table would produce exactly this, and
    its central problem is that it has no idea what the commands *mean*: a non-zero exit
    code is the only signal it has, so that is all the adapter can report.
    """

    def __init__(self, *, provider: str = "shell-agent", name: str = "aer-shell") -> None:
        self._provider = provider
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def protocol_version(self) -> str:
        return "1"

    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            provider=self._provider,
            agent_name="shell-agent",
            agent_version="0.3",
            model="shell-model",
            adapter_name=self._name,
            adapter_version="1",
        )

    def capabilities(self) -> AdapterCapabilities:
        # It sees commands. It cannot see whether the agent read anything it was given.
        return AdapterCapabilities(tool_events=True)

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        return AdapterSessionRequest(
            external_session_id=str(raw["run"]),
            task=str(raw["goal"]),
            task_type="shell",
            metadata={"shell": raw.get("shell", "bash")},
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        kind = raw.get("kind")
        common = {
            "external_event_id": _text(raw.get("seq")),
            "external_sequence": raw.get("seq") if isinstance(raw.get("seq"), int) else None,
            "external_timestamp": _timestamp(raw.get("at")),
        }
        if kind == "exec":
            exit_code = raw.get("exit_code")
            command = str(raw.get("cmd", ""))
            body = {
                "tool": command.split(" ")[0] if command else "shell",
                "success": exit_code == 0 if isinstance(exit_code, int) else None,
                "result": raw.get("stdout"),
            }
            if isinstance(exit_code, int) and exit_code != 0:
                body["error"] = {
                    "error_type": "shell.NonZeroExit",
                    "message": f"{command} exited {exit_code}",
                    "stack_trace": raw.get("stderr"),
                }
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.TOOL_CALL,
                    identity=self.identity(),
                    action=_action("shell", command, raw.get("cmd")),
                    **common,
                ),
                AgentExecutionEnvelope(
                    event_type=EventType.TOOL_RESULT,
                    identity=self.identity(),
                    payload=_compact(body),
                    **common,
                ),
            )
        if kind == "note":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.MODEL_RESULT,
                    identity=self.identity(),
                    payload={"summary": raw.get("text")},
                    **common,
                ),
            )
        if kind == "retry":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.RECOVERY_START,
                    identity=self.identity(),
                    payload={"reason": raw.get("why", "retry the command")},
                    **common,
                ),
            )
        if kind == "retry_done":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.RECOVERY_RESULT,
                    identity=self.identity(),
                    payload={"success": raw.get("ok") is True},
                    **common,
                ),
            )
        if kind == "cancelled":
            return ()
        raise UnsupportedAdapterEvent(f"shell adapter does not know the event kind {kind!r}")

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        return AdapterFinishRequest(
            status=RunStatus.SUCCESS if raw.get("result") == "ok" else RunStatus.FAILED,
            reason=str(raw.get("note", "")) or None,
        )


# ---------------------------------------------------------------------------
# a structured-tool-API style agent
# ---------------------------------------------------------------------------


class StructuredStyleAdapter:
    """Translates a phase-based vocabulary: ``begin``, ``action``, ``outcome``.

    A platform with a real tool API, where every action has an id, an outcome arrives
    correlated to it, and failures are structured objects. Everything it says is
    richer than the shell runner's -- and none of that reaches AER as anything other
    than the same protocol events.
    """

    def __init__(self, *, provider: str = "structured-agent", name: str = "aer-structured") -> None:
        self._provider = provider
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def protocol_version(self) -> str:
        return "1"

    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            provider=self._provider,
            agent_name="structured-agent",
            agent_version="2.1",
            model="structured-model",
            model_version="2026-05",
            adapter_name=self._name,
            adapter_version="1",
        )

    def capabilities(self) -> AdapterCapabilities:
        # It can correlate outcomes and it exposes a session id, so it can also say
        # whether a retrieved experience was picked up. It still cannot say whether the
        # experience *helped*.
        return AdapterCapabilities(
            tool_events=True,
            explicit_adoption_signal=True,
            session_linkage=True,
            human_feedback=True,
            external_verification=True,
        )

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        conversation = raw.get("conversation")
        if not isinstance(conversation, Mapping):
            raise AdapterError("a structured session start must carry a conversation")
        return AdapterSessionRequest(
            external_session_id=str(conversation["id"]),
            task=str(raw["instruction"]),
            task_type="structured",
            external_run_id=_text(conversation.get("run")),
            metadata={"workspace": conversation.get("workspace")},
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        phase = raw.get("phase")
        common = {
            "external_event_id": _text(raw.get("message_id")),
            "external_timestamp": _timestamp(raw.get("recorded_at")),
        }
        if phase == "action":
            action = raw["action"]
            assert isinstance(action, Mapping)
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.TOOL_CALL,
                    identity=self.identity(),
                    external_sequence=_sequence(action.get("ordinal")),
                    action=_action(
                        str(action.get("kind", "tool")),
                        str(action.get("tool", "unknown")),
                        action.get("parameters"),
                    ),
                    **common,
                ),
            )
        if phase == "outcome":
            outcome = raw.get("outcome")
            assert isinstance(outcome, Mapping)
            status = outcome.get("status")
            body: dict[str, object] = {
                "tool": _text(raw.get("tool")),
                "success": status == "ok",
                "result": outcome.get("payload"),
            }
            if status not in (None, "ok"):
                body["error"] = {
                    "error_type": outcome.get("code") or "structured.ActionFailed",
                    "message": outcome.get("detail") or "the action did not succeed",
                    "recoverable": outcome.get("retryable", True),
                }
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.TOOL_RESULT,
                    identity=self.identity(),
                    payload=_compact(body),
                    **common,
                ),
            )
        if phase == "repair":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.RECOVERY_START,
                    identity=self.identity(),
                    payload={"reason": raw.get("rationale", "apply the repair")},
                    metadata=_compact({"decision_summary": raw.get("rationale")}),
                    **common,
                ),
            )
        if phase == "repair_done":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.RECOVERY_RESULT,
                    identity=self.identity(),
                    payload={"success": raw.get("outcome") == "repaired"},
                    **common,
                ),
            )
        if phase == "annotation":
            return (
                AgentExecutionEnvelope(
                    event_type=EventType.HUMAN_FEEDBACK,
                    identity=self.identity(),
                    payload={"approved": raw.get("approved"), "comment": raw.get("note")},
                    **common,
                ),
            )
        if phase == "telemetry":
            return ()
        raise UnsupportedAdapterEvent(f"structured adapter does not know the phase {phase!r}")

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        outcome = raw.get("outcome")
        status = {
            "success": RunStatus.SUCCESS,
            "partial": RunStatus.PARTIAL_SUCCESS,
            "failed": RunStatus.FAILED,
        }.get(str(outcome))
        return AdapterFinishRequest(status=status, metadata=_compact({"outcome": outcome}))


# ---------------------------------------------------------------------------
# adapters built to misbehave
# ---------------------------------------------------------------------------


class CrashingAdapter(GenericAgentAdapter):
    """A reference adapter that raises while translating an event."""

    def __init__(self, *, error: BaseException | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.error = error or RuntimeError("the vendor SDK blew up")

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        if raw.get("type") == "explode":
            raise self.error
        return super().handle_event(raw)


class FutureProtocolAdapter(GenericAgentAdapter):
    """An adapter written against a protocol version this build does not speak."""

    @property
    def protocol_version(self) -> str:
        return "99"


class SilentAdapter(GenericAgentAdapter):
    """An adapter that declares no capabilities beyond translating events."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(tool_events=True)


class SloppyAdapter(GenericAgentAdapter):
    """An adapter that returns two envelopes with different external ids."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        envelopes = super().handle_event(raw)
        if not envelopes:
            return envelopes
        first = envelopes[0]
        return (
            first,
            first.model_copy(update={"external_event_id": "a-different-event"}),
        )


# ---------------------------------------------------------------------------
# driving the fakes
# ---------------------------------------------------------------------------


class ShellAgent:
    """A fake coding agent whose only vocabulary is commands and exit codes."""

    def __init__(
        self,
        ingestor: AdapterIngestor,
        adapter: ShellStyleAdapter,
        *,
        session_id: str = "shell-session",
        task: str = DEFAULT_TASK,
    ) -> None:
        self._ingestor = ingestor
        self._adapter = adapter
        self.session_id = session_id
        self.task = task
        self.handle: AdapterSessionHandle | None = None
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def connect(self) -> AdapterSessionHandle:
        self.handle = self._ingestor.open(
            self._adapter, {"run": self.session_id, "goal": self.task, "shell": "bash"}
        )
        return self.handle

    def run(
        self,
        command: str,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> list[IngestResult]:
        handle = self._handle()
        results = [
            self._ingestor.ingest(
                handle,
                {
                    "kind": "exec",
                    "cmd": command,
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                    "seq": self._next_seq(),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        ]
        return results

    def note(self, text: str) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(), {"kind": "note", "text": text, "seq": self._next_seq()}
        )

    def retry(self, why: str) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(), {"kind": "retry", "why": why, "seq": self._next_seq()}
        )

    def retry_done(self, *, ok: bool) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(), {"kind": "retry_done", "ok": ok, "seq": self._next_seq()}
        )

    def cancel(self) -> IngestResult:
        return self._ingestor.ingest(self._handle(), {"kind": "cancelled", "seq": self._next_seq()})

    def finish(self, *, ok: bool = True):
        return self._ingestor.close(
            self._handle(), {"result": "ok" if ok else "failed", "note": "done"}
        )

    def _handle(self) -> AdapterSessionHandle:
        if self.handle is None:  # pragma: no cover - a test that forgot to connect
            raise AssertionError("connect() must be called before sending events")
        return self.handle


class StructuredAgent:
    """The same fake coding agent, expressed in a phase-based tool API."""

    def __init__(
        self,
        ingestor: AdapterIngestor,
        adapter: StructuredStyleAdapter,
        *,
        session_id: str = "structured-session",
        task: str = DEFAULT_TASK,
    ) -> None:
        self._ingestor = ingestor
        self._adapter = adapter
        self.session_id = session_id
        self.task = task
        self.handle: AdapterSessionHandle | None = None
        self._ordinal = 0

    def _next_ordinal(self) -> int:
        self._ordinal += 1
        return self._ordinal

    def connect(self) -> AdapterSessionHandle:
        self.handle = self._ingestor.open(
            self._adapter,
            {
                "phase": "begin",
                "conversation": {"id": self.session_id, "workspace": "/srv/wp"},
                "instruction": self.task,
            },
        )
        return self.handle

    def call(self, tool: str, *, parameters: Mapping[str, object] | None = None) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(),
            {
                "phase": "action",
                "action": {
                    "id": f"act-{self._ordinal + 1}",
                    "kind": "api",
                    "tool": tool,
                    "parameters": dict(parameters or {}),
                    "ordinal": self._next_ordinal(),
                },
                "message_id": f"msg-{self._ordinal}-call",
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )

    def outcome(
        self, tool: str, *, ok: bool = True, payload: object = None, code: str | None = None
    ) -> IngestResult:
        outcome: dict[str, object] = {"status": "ok" if ok else "error", "payload": payload}
        if not ok:
            outcome["code"] = code or "http.403"
            outcome["detail"] = "the REST API refused the request"
            outcome["retryable"] = True
        return self._ingestor.ingest(
            self._handle(),
            {
                "phase": "outcome",
                "tool": tool,
                "outcome": outcome,
                "message_id": f"msg-{self._ordinal}-result",
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )

    def repair(self, rationale: str) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(),
            {
                "phase": "repair",
                "rationale": rationale,
                "message_id": f"msg-{self._ordinal}-repair",
            },
        )

    def repair_done(self, *, repaired: bool) -> IngestResult:
        return self._ingestor.ingest(
            self._handle(),
            {
                "phase": "repair_done",
                "outcome": "repaired" if repaired else "still broken",
                "message_id": f"msg-{self._ordinal}-repair-done",
            },
        )

    def annotate(self, *, approved: bool, note: str = "") -> IngestResult:
        return self._ingestor.ingest(
            self._handle(),
            {
                "phase": "annotation",
                "approved": approved,
                "note": note,
                "message_id": f"msg-{self._ordinal}-annotation",
            },
        )

    def telemetry(self, text: str) -> IngestResult:
        return self._ingestor.ingest(self._handle(), {"phase": "telemetry", "text": text})

    def finish(self, *, outcome: str = "success"):
        return self._ingestor.close(self._handle(), {"outcome": outcome})

    def _handle(self) -> AdapterSessionHandle:
        if self.handle is None:  # pragma: no cover - a test that forgot to connect
            raise AssertionError("connect() must be called before sending events")
        return self.handle


# ---------------------------------------------------------------------------
# scenario helpers
# ---------------------------------------------------------------------------


def adopt(
    runtime: AER,
    handle: AdapterSessionHandle,
    tracked: TrackedRetrievalResult,
    *,
    experience_id: str,
) -> None:
    """Record an explicit adoption the way an integration would."""
    runtime.adapter_ingestor.record_usage_signal(
        handle, tracked, experience_id=experience_id, signal=UsageSignal.ADOPTED
    )


def outcomes(*results: IngestResult) -> list[str]:
    """The outcome of each delivery, for assertions that span a whole stream."""
    return [result.outcome.value for result in results]


def applied(result: IngestResult) -> bool:
    """Whether a delivery wrote anything."""
    return result.outcome is AdapterIngestOutcome.APPLIED


# ---------------------------------------------------------------------------
# small input helpers (the adapters above need them; the library's are private)
# ---------------------------------------------------------------------------


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sequence(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _action(kind: str, name: str, arguments: object) -> AgentAction:
    summary = arguments if isinstance(arguments, str) else None
    return AgentAction(kind=kind, name=name, tool_name=name, input_summary=summary)


def _compact(mapping: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in mapping.items() if value is not None}
