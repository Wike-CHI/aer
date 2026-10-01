"""Codex hook payloads into AER protocol envelopes.

Everything vendor-specific about Codex lives in this module, and nothing outside it
knows a Codex field name (round-8 section 1). The functions are pure: payload in,
envelopes or values out, no storage, no runtime, no state. That is what makes the
mapping testable against captured payloads rather than only against a live session.

Three rules shape the translation, and each is a refusal to guess:

**Only what was observed is mapped.** :data:`ENVELOPE_FOR` lists the hook events whose
shape was captured from the installed CLI. Everything else -- including events that
plainly exist and carry interesting information -- is delivered as *no envelope*, and
recorded as ignored. A field named in a binary's serialization tables is not a payload;
building on one would produce a mapping that looks tested and has never met a real
event (sections 13-19).

**Tool outcome is tri-state.** ``success`` is ``True``, ``False`` or ``None``, where
``None`` means the payload did not say. Reporting "no failure" for a payload that never
mentioned an outcome would put a claim into the trace that nobody made, and it would
suppress the error record that a real failure deserves (section 15).

**Identity is deterministic.** Codex does not send a stable event id for every hook, so
:func:`external_event_id` derives one from the session, the event, any correlation id,
and a digest of the event's own identifying fields. Two deliveries of one event collapse
to one AER event; two genuinely different events must not (sections 28-29).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from aer.adapter.protocol import (
    AgentAction,
    AgentExecutionEnvelope,
    AgentIdentity,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.sanitization import redact
from aer.runtime.serialization import JsonObject, to_json_object

__all__ = [
    "CODEX_AGENT_NAME",
    "CODEX_PROVIDER",
    "ENVELOPE_FOR",
    "CodexHookEvent",
    "SessionStartSource",
    "event_name",
    "external_event_id",
    "finish_status",
    "session_id",
    "task_summary",
    "tool_envelopes",
    "turn_id",
]

#: The vendor this adapter speaks for. Part of the session-mapping and ledger keys, so
#: it is also what makes two adapters aimed at Codex collide on purpose (round-8
#: section 19).
CODEX_PROVIDER = "openai"
CODEX_AGENT_NAME = "codex"


class CodexHookEvent(StrEnum):
    """The hook events Codex 0.155.1 defines.

    A closed vocabulary on the Codex side. An event name outside it is refused rather
    than ignored: a new hook in a newer CLI means this adapter was not written for that
    CLI, and silently dropping its events would make the upgrade look clean while the
    trace quietly lost a dimension (section 46).
    """

    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    STOP = "Stop"
    SESSION_END = "SessionEnd"
    INTERRUPT = "Interrupt"


class SessionStartSource(StrEnum):
    """Why a Codex session is starting.

    Captured from the CLI: ``SessionStart.source``. Kept because it is the difference
    between "a new task began" and "the same task continued after a compaction", and a
    trace that cannot tell those apart cannot say whether a run was interrupted.
    """

    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"
    FORK = "fork"


#: Hook events mapped to protocol envelopes, and how.
#:
#: Deliberately two entries. ``PreToolUse`` and ``PostToolUse`` are the two events whose
#: payload shape was named by the CLI's own serializer *and* whose AER meaning is
#: unambiguous. The other ten are delivered as nothing and counted as ignored -- see the
#: module docstring and ``docs/DECISIONS.md`` D-091.
ENVELOPE_FOR: Mapping[CodexHookEvent, EventType] = {
    CodexHookEvent.PRE_TOOL_USE: EventType.TOOL_CALL,
    CodexHookEvent.POST_TOOL_USE: EventType.TOOL_RESULT,
}


def event_name(payload: Mapping[str, object]) -> CodexHookEvent:
    """The hook event this payload belongs to.

    Raises:
        AdapterProtocolError: the payload carries no ``hook_event_name``. Codex always
            sends it, so its absence means this is not a Codex hook delivery at all --
            almost certainly a misconfigured hook command.
        UnsupportedAdapterEvent: the name is not one this adapter was written for.
    """
    raw = payload.get("hook_event_name")
    if not isinstance(raw, str) or not raw.strip():
        raise AdapterProtocolError(
            "A Codex hook payload must carry 'hook_event_name'; without it there is "
            "nothing to say what happened"
        )
    try:
        return CodexHookEvent(raw)
    except ValueError as exc:
        known = ", ".join(member.value for member in CodexHookEvent)
        raise UnsupportedAdapterEvent(
            f"Codex reported hook event {raw!r}, which this adapter does not know. It "
            f"was written for: {known}. A newer Codex CLI may have added a hook; "
            "upgrade the adapter rather than let its events go missing."
        ) from exc


def session_id(payload: Mapping[str, object]) -> str:
    """The Codex session this delivery belongs to.

    The AER run is keyed on this, so a payload without it cannot be placed anywhere and
    is refused rather than filed under a guess.
    """
    value = payload.get("session_id")
    if not isinstance(value, str) or not value.strip():
        raise AdapterProtocolError("A Codex hook payload must carry 'session_id'")
    return value


def turn_id(payload: Mapping[str, object]) -> str | None:
    """The Codex turn this delivery belongs to, when it says."""
    value = payload.get("turn_id")
    return value if isinstance(value, str) and value.strip() else None


def tool_use_id(payload: Mapping[str, object]) -> str | None:
    """The tool invocation this delivery correlates to, when it says.

    Section 14 asks for the tool call and its result to be linked. Codex names the
    field ``tool_use_id``; whether it is present on every ``PostToolUse`` is not
    something this adapter has been able to observe, so a missing id degrades the link
    instead of failing the delivery (see the coverage report).
    """
    value = payload.get("tool_use_id")
    return value if isinstance(value, str) and value.strip() else None


def event_time(payload: Mapping[str, object]) -> datetime | None:
    """The payload's own timestamp, when it has one and it is unambiguous.

    A naive timestamp is dropped rather than assumed local: an event from another
    timezone would otherwise be shifted silently, and AER's own clock is the
    authoritative one anyway (round-8 section 24).
    """
    for key in ("timestamp", "occurred_at", "recorded_at"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else None
    return None


def task_summary(prompt: str, *, limit: int = 200) -> str:
    """A one-line task description derived from a user prompt.

    Section 17 is explicit that the full prompt must not be kept by default. This keeps
    the shape a human would recognise as "what was asked" -- first line, collapsed, cut
    at ``limit``.

    Credentials are redacted here rather than downstream, because this string becomes
    the run's ``task_description`` and **that path has no sanitizer of its own**: the
    ingest-time redaction covers event payloads and metadata, and a prompt that reached
    the task field unredacted would sit in the store forever with the run -- the one
    field a reader looks at first. Section 40 asks for exactly this reuse of the M8
    redaction rules.
    """
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    collapsed = " ".join(first.split())
    if not collapsed:
        return "Codex session (empty prompt)"
    collapsed = redact(collapsed)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def external_event_id(event: CodexHookEvent, payload: Mapping[str, object]) -> str:
    """A deterministic identity for one delivery, derived when Codex does not give one.

    Section 28 records that Codex hooks have been observed firing more than once; the
    ledger can only collapse duplicates if the same delivery always hashes the same.
    Section 29 adds the other half of the requirement: the fingerprint must not merge
    two events that are genuinely different, which is why the digest covers the fields
    that distinguish them (the turn, the tool invocation, the event's own content) and
    not merely the fact that they share a session and a name.
    """
    parts: list[object] = [
        CODEX_PROVIDER,
        event.value,
        payload.get("session_id"),
        payload.get("turn_id"),
        payload.get("tool_use_id"),
        payload.get("source"),
        payload.get("reason"),
    ]
    if event is CodexHookEvent.USER_PROMPT_SUBMIT:
        parts.append(_digest(payload.get("prompt")))
    if event in (CodexHookEvent.PRE_TOOL_USE, CodexHookEvent.POST_TOOL_USE):
        parts.append(payload.get("tool_name"))
        parts.append(_digest(payload.get("tool_input")))
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"codex-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def finish_status(payload: Mapping[str, object]) -> RunStatus | None:
    """The terminal status a ``SessionEnd`` payload states, or ``None``.

    Section 10: a session ending is not a task succeeding. The captured payload carries
    a ``reason`` rather than an outcome, and the one observed value was ``other`` --
    which says nothing about whether the work was done. So the mapping is deliberately
    narrow, and anything it does not recognise returns ``None``, leaving the caller to
    decide explicitly rather than being handed an invented verdict.

    ``None`` means **Codex declared nothing**, which is not the same as "the run stays
    open": the caller closes it ``INCONCLUSIVE``, AER's status for a finished run whose
    outcome nobody stated (round-8.1.1, D-100).

    ``RunStatus`` is the *agent's declaration*, not a verification, so a recognised
    completion reason maps to ``SUCCESS`` without any claim about the world: an
    independent verifier is still required before anything is called verified.
    """
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return None
    normalized = reason.strip().casefold()
    if normalized in {"complete", "completed", "success", "done"}:
        return RunStatus.SUCCESS
    if normalized in {"error", "failed", "failure"}:
        return RunStatus.FAILED
    if normalized in {"interrupt", "interrupted", "aborted", "cancelled", "canceled"}:
        return RunStatus.ABORTED
    # ``other`` lands here, by design: the session ended and nothing was claimed.
    return None


def tool_envelopes(
    event: CodexHookEvent,
    payload: Mapping[str, object],
    *,
    identity: AgentIdentity,
    common: Mapping[str, object],
) -> Sequence[AgentExecutionEnvelope]:
    """Translate a tool-focused hook into protocol envelopes.

    ``PreToolUse`` becomes a call and ``PostToolUse`` a result. Both shapes are captured
    from Codex 0.155.1 and are exactly:

    ``PreToolUse``
        ``session_id``, ``turn_id``, ``transcript_path``, ``cwd``, ``hook_event_name``,
        ``model``, ``permission_mode``, ``tool_name``, ``tool_input``, ``tool_use_id``
    ``PostToolUse``
        the same, plus ``tool_response``

    Two consequences of that shape are worth stating here rather than leaving to be
    rediscovered:

    * **``tool_use_id`` is present on both events**, and a captured run showed a 1:1
      match between the calls and the results, so the correlation section 8 asks for is
      available rather than "linkage unavailable";
    * **``tool_response`` is a plain string**, not an object. For ``Bash`` it is the
      command's output; for ``apply_patch`` it is a short report. There is no
      ``exit_code``, no ``success`` and no ``error`` field anywhere in the payload, so a
      tool that failed is distinguishable only by the *text* of its output. AER does not
      read that text: deriving a failure from prose is what section 52 forbids, and a
      fabricated ``ERROR`` would be worse than a missing one. ``success`` therefore stays
      ``None`` -- the honest "the payload stated no outcome" -- and no error envelope is
      produced (D-097).
    """
    name = _text(payload.get("tool_name")) or "unknown-tool"
    tool_input = payload.get("tool_input")

    if event is CodexHookEvent.PRE_TOOL_USE:
        return (
            AgentExecutionEnvelope(
                event_type=EventType.TOOL_CALL,
                identity=identity,
                external_event_id=external_event_id(event, payload),
                external_session_id=_text(payload.get("session_id")),
                external_timestamp=event_time(payload),
                action=AgentAction(
                    kind="tool",
                    name=name,
                    tool_name=name,
                    input_summary=_summarize(tool_input),
                ),
                payload=to_json_object({"tool": name, "tool_use_id": tool_use_id(payload)}),
                metadata=to_json_object(common),
            ),
        )

    response = payload.get("tool_response")
    failure = _tool_failure(name, response)
    # Tri-state on purpose: a payload that never mentioned an outcome produces neither
    # "it worked" nor "it failed". Recording either would be a claim nobody made.
    success = False if failure is not None else (True if _has_outcome(response) else None)
    result_body: JsonObject = {
        "tool": name,
        "tool_use_id": tool_use_id(payload),
        "success": success,
        "result": _summarize(response) if response is not None else None,
    }
    if failure is not None:
        # The failure travels *inside* the result rather than as a second envelope. The
        # ingest path turns a result carrying an ``error`` into ERROR-then-TOOL_RESULT,
        # which is the ordering AER wants; emitting an ERROR envelope as well would make
        # the ingest path record the same failure twice, because it cannot tell an
        # explicit failure envelope from one it is about to synthesise.
        result_body["error"] = {
            "error_type": failure["error_type"],
            "message": failure["message"],
        }
    return (
        AgentExecutionEnvelope(
            event_type=EventType.TOOL_RESULT,
            identity=identity,
            external_event_id=external_event_id(event, payload),
            external_session_id=_text(payload.get("session_id")),
            external_timestamp=event_time(payload),
            payload=to_json_object(
                {key: value for key, value in result_body.items() if value is not None}
            ),
            metadata=to_json_object(common),
        ),
    )


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------


def _tool_failure(name: str, response: object) -> dict[str, str] | None:
    """Describe a tool failure, or ``None`` when the payload does not report one.

    **For the tool shapes captured from Codex 0.155.1 this always returns ``None``**, and
    that is the correct answer rather than a gap: ``tool_response`` is a string, and a
    tool that failed is distinguishable only by the words in its output. Reading those
    words is what section 52 forbids -- and it would be unreliable besides, since
    ``"passed"`` appears in the output of a failing command's ``grep`` just as readily as
    in a passing one.

    The mapping branch below is kept as a narrow tolerance for a tool that returns a
    *structured* response with an explicit outcome. No such tool was observed; it is
    three lines and it is labelled as unobserved, which is the difference between
    tolerance and a guess. Every top-level ``exit_code``/``error``/``status`` check that
    an earlier revision had was **deleted after capture**: those keys do not exist in the
    real payload, so the checks were dead code that made the adapter look more capable
    than the hook surface allows (section 7 of the round-8.1 completion brief).
    """
    if not isinstance(response, Mapping):
        return None
    error = response.get("error")
    if error is not None:
        return {
            "error_type": _text(response.get("error_type")) or "codex.ToolError",
            "message": _text(error) or _summarize(error) or f"{name} reported an error",
        }
    if response.get("success") is False:
        return {
            "error_type": "codex.ToolFailure",
            "message": _summarize(response.get("message")) or f"{name} did not succeed",
        }
    return None


def _has_outcome(response: object) -> bool:
    """Whether the payload said anything at all about how the tool went.

    Evaluated against captured payloads: for Codex 0.155.1 this is ``False`` for every
    tool observed, because ``tool_response`` is a string. It answers ``True`` only for a
    structured response carrying an explicit outcome -- the same narrow tolerance
    :func:`_tool_failure` documents.
    """
    if not isinstance(response, Mapping):
        return False
    return "error" in response or "success" in response


def _summarize(value: object, *, limit: int = 500) -> str | None:
    """A short, single-line rendering of an arbitrary payload fragment.

    Section 13 asks for a sanitized input summary rather than the whole tool input; the
    same reasoning applies to a result, which for a shell command can be a build log.
    The M8 ingest path caps and redacts whatever this produces.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(value)
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _digest(value: object) -> str:
    """A stable digest of one payload fragment, for the fingerprint."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
