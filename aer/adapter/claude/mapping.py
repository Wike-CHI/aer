"""Claude Code hook payloads into AER protocol envelopes.

Everything vendor-specific about Claude Code lives in this module, and nothing
outside it knows a Claude Code field name (round-8 section 1). The functions are
pure: payload in, envelopes or values out, no storage, no runtime, no state, which
is what makes the mapping testable against captured payloads rather than only
against a live session.

The evidence discipline is the same one the Codex adapter established: **only what
was observed is claimed as observed**. The event vocabulary below was extracted
from the string table of the installed CLI (``claude.exe`` 2.1.310 -- the Bun
snapshot carries the hook event names and the payload keys verbatim), so the
*names* are captured from the binary, not guessed. Whether each event's *payload
shape* matches the mapping is recorded per-cell in
:mod:`~aer.adapter.claude.coverage` and starts at ``CONTRACT_ONLY`` until
``scripts/probe_claude_hooks.py`` captures a real delivery.

Two translation rules are worth stating up front:

**A turn is the unit of work, not the session.** A Claude Code session is a
conversation that survives many prompts; an AER Run is one task execution. So a
``UserPromptSubmit`` while a run is still open ends that run ``INCONCLUSIVE`` (the
user moved on without declaring an outcome) and opens the next one. Which payload
closes a run is declared once here -- :data:`_CLOSES_A_RUN` -- so the hook and the
adapter cannot drift apart.

**No payload declares a task outcome.** ``Teardown`` / ``SessionEnd`` carry only an
exit *cause* (clear, logout, prompt_input_exit, other), and ``Stop`` carries a turn
*mechanism* (end_turn, tool_use, ...). Neither says whether the task succeeded; the
mapping therefore recognises no outcome at all, and every close is
``INCONCLUSIVE`` -- the status AER defines for exactly that (round-8.1.1, D-100).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum

from aer.adapter.protocol import (
    AgentAction,
    AgentExecutionEnvelope,
    AgentIdentity,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType
from aer.runtime.sanitization import redact
from aer.runtime.serialization import JsonObject, to_json_object

__all__ = [
    "CLAUDE_AGENT_NAME",
    "CLAUDE_PROVIDER",
    "ENVELOPE_FOR",
    "SESSION_END_REASONS",
    "ClaudeHookEvent",
    "closes_run",
    "event_name",
    "external_event_id",
    "session_id",
    "task_summary",
    "tool_envelopes",
]

#: The vendor this adapter speaks for. Part of the session-mapping and ledger keys,
#: so two adapters aimed at Claude Code collide on purpose (round-8 section 19).
CLAUDE_PROVIDER = "anthropic"
CLAUDE_AGENT_NAME = "claude-code"

#: Exit causes stated by SessionEnd, captured from a real payload. The one observed
#: value so far is ``other`` -- on a turn that *succeeded* -- which is why no value
#: in this set is ever read as a task outcome (D-099's parallel, recorded in the
#: coverage notes).
SESSION_END_REASONS = frozenset({"clear", "logout", "prompt_input_exit", "shutdown", "other"})


class ClaudeHookEvent(StrEnum):
    """The hook events Claude Code 2.1.310's own validator accepts.

    The authoritative list was captured from the CLI itself: when this adapter's
    first probe offered it an event it does not know, the CLI printed its full
    valid-event list in the rejection message. Two traps in that history are worth
    keeping visible:

    * "Teardown" appeared in the binary's string table but is **not** a hook event
      -- the validator rejected it. The string table names internals too; only the
      validator's word counts (recorded as D-101).
    * "PostToolBatch" is the batch event's name; "PostToolUseBatch" (a plausible
      guess from the string table) is rejected the same way.

    A closed vocabulary on the Claude Code side, which is why an event name outside
    it is refused rather than ignored: a new hook in a newer CLI means this adapter
    was not written for that CLI, and silently dropping its events would make the
    upgrade look clean while the trace quietly lost a dimension.
    """

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    POST_TOOL_BATCH = "PostToolBatch"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    USER_PROMPT_EXPANSION = "UserPromptExpansion"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_MODEL_SWITCH = "PreModelSwitch"
    POST_MODEL_SWITCH = "PostModelSwitch"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SETUP = "Setup"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    ELICITATION = "Elicitation"
    ELICITATION_RESULT = "ElicitationResult"
    CONFIG_CHANGE = "ConfigChange"
    WORKTREE_CREATE = "WorktreeCreate"
    WORKTREE_REMOVE = "WorktreeRemove"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    CWD_CHANGED = "CwdChanged"
    FILE_CHANGED = "FileChanged"


#: Hook events mapped to protocol envelopes, and how.
#:
#: All three tool events are captured shapes (scripts/probe_claude_hooks.py,
#: 2.1.310). ``PostToolUseFailure`` is the CLI's *dedicated* failure channel: it
#: carries a structured ``error`` and no ``tool_response``, and it exists precisely
#: because ``PostToolUse``'s ``tool_response`` states no outcome (captured:
#: ``{"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"}`` -- none of
#: which is a success/failure verdict). That split is the structural answer to the
#: question D-097 left open for Codex, and it is recorded as D-102.
ENVELOPE_FOR: Mapping[ClaudeHookEvent, EventType] = {
    ClaudeHookEvent.PRE_TOOL_USE: EventType.TOOL_CALL,
    ClaudeHookEvent.POST_TOOL_USE: EventType.TOOL_RESULT,
    ClaudeHookEvent.POST_TOOL_USE_FAILURE: EventType.TOOL_RESULT,
}

#: Hook events that end the current run when they arrive while one is open.
#: ``SessionEnd`` is the single terminal authority (D-101); ``Stop`` is deliberately
#: absent -- it is turn-level and carries no outcome (captured payload agrees: it
#: fired on a failed provider call too). ``UserPromptSubmit`` closes the *previous*
#: run only when the hook routes it that way; the event itself opens runs.
_CLOSES_A_RUN = frozenset({ClaudeHookEvent.SESSION_END})


def event_name(payload: Mapping[str, object]) -> ClaudeHookEvent:
    """The hook event this payload belongs to.

    Raises:
        AdapterProtocolError: the payload carries no ``hook_event_name``. Claude
            Code always sends it, so its absence means this is not a Claude Code
            hook delivery at all -- almost certainly a misconfigured hook command.
        UnsupportedAdapterEvent: the name is not one this adapter was written for.
    """
    raw = payload.get("hook_event_name")
    if not isinstance(raw, str) or not raw.strip():
        raise AdapterProtocolError(
            "A Claude Code hook payload must carry 'hook_event_name'; without it "
            "there is nothing to say what happened"
        )
    try:
        return ClaudeHookEvent(raw)
    except ValueError as exc:
        known = ", ".join(member.value for member in ClaudeHookEvent)
        raise UnsupportedAdapterEvent(
            f"Claude Code reported hook event {raw!r}, which this adapter does not "
            f"know. It was written for: {known}. A newer CLI may have added a hook; "
            "upgrade the adapter rather than let its events go missing."
        ) from exc


def session_id(payload: Mapping[str, object]) -> str:
    """The Claude Code session this delivery belongs to.

    The AER run is keyed on this, so a payload without it cannot be placed anywhere
    and is refused rather than filed under a guess.
    """
    value = payload.get("session_id")
    if not isinstance(value, str) or not value.strip():
        raise AdapterProtocolError("A Claude Code hook payload must carry 'session_id'")
    return value


def tool_use_id(payload: Mapping[str, object]) -> str | None:
    """The tool invocation this delivery correlates to, when it says.

    ``tool_use_id`` is present in the 2.1.310 string table on both tool hooks, so
    the call/result correlation the protocol asks for is expected to be available;
    a payload that omits it degrades the link instead of failing the delivery, and
    the coverage report is where that gap would show.
    """
    value = payload.get("tool_use_id")
    return value if isinstance(value, str) and value.strip() else None


def task_summary(prompt: str, *, limit: int = 200) -> str:
    """A one-line task description derived from a user prompt.

    The full prompt must not be kept by default (the same rule the Codex adapter
    states for section 17). This keeps the shape a human would recognise as "what
    was asked" -- first line, collapsed, cut at ``limit``.

    Credentials are redacted here rather than downstream, because this string
    becomes the run's ``task_description`` and **that path has no sanitizer of its
    own**: a prompt that reached the task field unredacted would sit in the store
    forever with the run -- the one field a reader looks at first.
    """
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    collapsed = " ".join(first.split())
    if not collapsed:
        return "Claude Code session (empty prompt)"
    collapsed = redact(collapsed)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def external_event_id(event: ClaudeHookEvent, payload: Mapping[str, object]) -> str:
    """A deterministic identity for one delivery, derived because the CLI sends none.

    Claude Code hooks carry no event id of their own, so a redelivery cannot be
    recognised as one without a stable fingerprint. ``prompt_id`` (captured on every
    2.1.310 payload) is the correlation the CLI actually provides: same prompt, same
    id, across all of that turn's hook events. The digest covers the fields that
    distinguish two events sharing a session and a name -- the tool invocation, the
    tool input -- so genuine repeats collapse to one AER event while genuinely
    different events never merge (round-8 sections 28-29).
    """
    parts: list[object] = [
        CLAUDE_PROVIDER,
        event.value,
        payload.get("session_id"),
        payload.get("prompt_id"),
        payload.get("tool_use_id"),
    ]
    if event is ClaudeHookEvent.USER_PROMPT_SUBMIT:
        parts.append(_digest(payload.get("prompt")))
    if event in (
        ClaudeHookEvent.PRE_TOOL_USE,
        ClaudeHookEvent.POST_TOOL_USE,
        ClaudeHookEvent.POST_TOOL_USE_FAILURE,
    ):
        parts.append(payload.get("tool_name"))
        parts.append(_digest(payload.get("tool_input")))
    if event is ClaudeHookEvent.SESSION_END:
        parts.append(payload.get("reason"))
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"claude-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def tool_envelopes(
    event: ClaudeHookEvent,
    payload: Mapping[str, object],
    *,
    identity: AgentIdentity,
    common: Mapping[str, object],
) -> Sequence[AgentExecutionEnvelope]:
    """Translate a tool-focused hook into protocol envelopes.

    All three shapes are captured from 2.1.310, not assumed:

    ``PreToolUse``
        ``tool_name``, ``tool_input`` (structured), ``tool_use_id`` -- a clean
        ``TOOL_CALL``;
    ``PostToolUse``
        the same plus ``tool_response`` and ``duration_ms``. The captured
        ``tool_response`` for ``Bash`` is ``{"stdout", "stderr", "interrupted",
        "isImage", "noOutputExpected"}`` -- there is **no outcome field in it**, so
        ``success`` stays ``None`` (the honest "the payload stated no verdict").
        Failures do not arrive here at all;
    ``PostToolUseFailure``
        the CLI's dedicated failure channel (D-102): structured ``error`` (a string),
        ``is_interrupt``, ``duration_ms``, and *no* ``tool_response``. It maps to a
        ``TOOL_RESULT`` carrying ``success=False`` and the error, which the ingest
        path expands into the ERROR-then-TOOL_RESULT ordering AER wants.

    Reading failure out of ``PostToolUse``'s prose is what round-8 section 52
    forbids; on this hook surface it is also unnecessary, because the prose-shaped
    gap has a structural sibling.
    """
    name = _text(payload.get("tool_name")) or "unknown-tool"
    tool_input = payload.get("tool_input")

    if event is ClaudeHookEvent.PRE_TOOL_USE:
        return (
            AgentExecutionEnvelope(
                event_type=EventType.TOOL_CALL,
                identity=identity,
                external_event_id=external_event_id(event, payload),
                external_session_id=_text(payload.get("session_id")),
                action=AgentAction(
                    kind="tool",
                    name=name,
                    tool_name=name,
                    input_summary=_summarize(tool_input),
                ),
                payload=to_json_object(
                    {
                        "tool": name,
                        "tool_use_id": tool_use_id(payload),
                        "duration_ms": payload.get("duration_ms"),
                    }
                ),
                metadata=to_json_object(common),
            ),
        )

    if event is ClaudeHookEvent.POST_TOOL_USE_FAILURE:
        error = payload.get("error")
        result_body: JsonObject = {
            "tool": name,
            "tool_use_id": tool_use_id(payload),
            "success": False,
            "duration_ms": _millis(payload.get("duration_ms")),
            "error": {
                "error_type": "claude.ToolFailure",
                "message": _text(error) or _UNDESCRIBED_FAILURE,
            },
        }
        return (
            AgentExecutionEnvelope(
                event_type=EventType.TOOL_RESULT,
                identity=identity,
                external_event_id=external_event_id(event, payload),
                external_session_id=_text(payload.get("session_id")),
                payload=to_json_object(
                    {key: value for key, value in result_body.items() if value is not None}
                ),
                metadata=to_json_object(common),
            ),
        )

    response = payload.get("tool_response")
    # ``success`` stays ``None`` on purpose: the captured tool_response states no
    # verdict (D-102). ``interrupted`` is not a failure claim either -- it says the
    # tool was cut off, not that it failed, and recording it as one would put a
    # claim into the trace that nobody made.
    result_body = {
        "tool": name,
        "tool_use_id": tool_use_id(payload),
        "success": None,
        "duration_ms": _millis(payload.get("duration_ms")),
        "result": _summarize(response) if response is not None else None,
    }
    return (
        AgentExecutionEnvelope(
            event_type=EventType.TOOL_RESULT,
            identity=identity,
            external_event_id=external_event_id(event, payload),
            external_session_id=_text(payload.get("session_id")),
            payload=to_json_object(
                {key: value for key, value in result_body.items() if value is not None}
            ),
            metadata=to_json_object(common),
        ),
    )


def closes_run(event: ClaudeHookEvent) -> bool:
    """Whether this event is one of the run-closing events, by declaration."""
    return event in _CLOSES_A_RUN


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------

#: The message used when PostToolUseFailure carries no usable ``error`` text. Same
#: rule as the ingest path's placeholder: a structured failure channel that says
#: nothing is recorded as "undescribed", never guessed at.
_UNDESCRIBED_FAILURE = "the tool failed without describing the failure"


def _summarize(value: object, *, limit: int = 500) -> str | None:
    """A short, single-line rendering of an arbitrary payload fragment.

    The protocol asks for a sanitized input summary rather than the whole tool
    input; the same reasoning applies to a result, which for a shell command can be
    a build log. The M8 ingest path caps and redacts whatever this produces.
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


def _millis(value: object) -> int | float | None:
    """A duration field as a JSON number, or ``None`` when the payload did not send one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None
