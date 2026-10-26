"""The Claude Code adapter: one Agent's vocabulary, translated into the protocol.

    Claude Code hook payload  ->  this module  ->  AgentExecutionEnvelope  ->  AdapterIngestor

It holds no state and touches no storage. Everything it knows about Claude Code is
a translation table and a payload field name, and everything it does with them goes
through :mod:`aer.adapter.claude.mapping`. An adapter that remembered something
between deliveries would lose it the moment the CLI restarted the process, which is
the failure the persisted session mapping exists to avoid (round-8 sections 9, 21).

Three decisions are worth stating, because each one is a refusal to overclaim about
a CLI that was measured rather than assumed:

**The environment identifies the agent, but only up to a point.** ``agent_version``
is the installed CLI's version when it can be read, and ``"unknown"`` otherwise --
never a guessed number. No payload field is trusted to name the model until one is
captured doing so; section 4 is explicit that a model name must not be inferred, and
section 3 that it must never decide what kind of agent this is.

**SessionStart does not open the run.** Claude Code's ``SessionStart`` payload
carries no task (session id, transcript path, cwd, event name, and source -- no
prompt). A run opened from it would be labelled with a placeholder for the rest of
its life, which is exactly the field AER distils experience *about* (the same
decision the Codex adapter records as D-091). The run is established by the first
``UserPromptSubmit``, whose prompt says what was asked.

**No hook declares a task outcome.** ``SessionEnd`` -- the single terminal
authority (D-101: ``Teardown`` turned out not to be a hook event at all) -- states
only *why the session ended*, and ``Stop`` only *how the turn stopped*. So every
close is ``INCONCLUSIVE``: the session is over and no participant said how the work
went, which leaves the run open to independent verification instead of being
mislabelled a failure (round-8.1.1, D-100, and the Codex adapter's D-099).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from aer.adapter.claude import mapping
from aer.adapter.claude.coverage import (
    VERIFIED_CLAUDE_VERSION,
    ClaudeHookCoverage,
    ClaudeMode,
    default_coverage,
)
from aer.adapter.protocol import (
    AER_ADAPTER_PROTOCOL_VERSION,
    AdapterCapabilities,
    AdapterFinishRequest,
    AdapterSessionRequest,
    AgentExecutionEnvelope,
    AgentIdentity,
)
from aer.exceptions import AdapterProtocolError
from aer.runtime.enums import RunStatus
from aer.runtime.serialization import to_json_object

__all__ = ["ADAPTER_NAME", "ADAPTER_VERSION", "ClaudeCodeAdapter", "detect_claude_version"]

#: Stable adapter identifier. Recorded on every run so "which integration produced
#: this?" stays answerable when there are several (round-8 section 38).
ADAPTER_NAME = "aer-claude-code"
#: Version of *this adapter*, not of Claude Code and not of the protocol.
ADAPTER_VERSION = "1"

#: The task a run gets when a payload can describe a session but not what was asked.
#: Only reachable through :meth:`ClaudeCodeAdapter.start` being called with a payload
#: that has no prompt, which the hook avoids doing; it exists so the refusal is
#: explicit rather than an exception in the middle of a hook delivery.
_UNKNOWN_TASK = "Claude Code session (task not stated by any hook payload)"

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def detect_claude_version(*, timeout: float = 10.0) -> str:
    """Ask the installed CLI for its version, or report ``"unknown"``.

    Called once when the adapter is built rather than per delivery: a subprocess on
    the hook's synchronous path is exactly the kind of cost round-8 section 8 rules
    out. A failure to detect is reported as ``"unknown"`` and never guessed -- an
    adapter that claims ``2.1.310`` because that is what it was written against
    would make a version mismatch invisible in the data.

    ``shutil.which`` is used rather than the bare name because on Windows ``claude``
    is an npm shim: ``CreateProcess`` performs no PATHEXT lookup, so a bare name
    raises ``FileNotFoundError`` even though the command works in a shell (measured
    on this exact failure for the Codex shim, same packaging here).

    Args:
        timeout: Seconds to wait for ``claude --version``.
    """
    resolved = shutil.which("claude") or "claude"
    command = [resolved, "--version"]
    for use_shell in (False, True):
        try:
            completed = subprocess.run(
                "claude --version" if use_shell else command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=use_shell,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        text = (completed.stdout or completed.stderr or "").strip()
        match = _VERSION_RE.search(text)
        if match:
            return match.group(1)
        if text:
            return text
    return "unknown"


class ClaudeCodeAdapter:
    """Translates Claude Code lifecycle hooks into AER protocol envelopes."""

    def __init__(
        self,
        *,
        agent_version: str | None = None,
        model: str | None = None,
        adapter_name: str = ADAPTER_NAME,
        adapter_version: str = ADAPTER_VERSION,
        coverage: ClaudeHookCoverage | None = None,
        detect_version: bool = True,
    ) -> None:
        self._agent_version = (
            agent_version
            if agent_version is not None
            else (detect_claude_version() if detect_version else "unknown")
        )
        # Only recorded when a caller supplies it, i.e. when a payload actually
        # carried one. Section 4: never inferred.
        self._model = model
        self._adapter_name = adapter_name
        self._adapter_version = adapter_version
        self._coverage = coverage or default_coverage()

    # -- AgentAdapter ------------------------------------------------------

    @property
    def name(self) -> str:
        """Stable adapter identifier."""
        return self._adapter_name

    @property
    def protocol_version(self) -> str:
        """The protocol version this adapter was written against."""
        return AER_ADAPTER_PROTOCOL_VERSION

    @property
    def coverage(self) -> ClaudeHookCoverage:
        """What this adapter has been verified to see (sections 37-38)."""
        return self._coverage

    @property
    def tested_claude_versions(self) -> tuple[str, ...]:
        """Claude Code versions this adapter's mapping was measured against.

        One entry, not a range: a claim about versions nobody ran is a claim about
        a guess, and the probe exists so that extending this tuple is cheap.
        """
        return (self._coverage.claude_version,)

    def identity(self) -> AgentIdentity:
        """Who this adapter speaks for."""
        return AgentIdentity(
            provider=mapping.CLAUDE_PROVIDER,
            agent_name=mapping.CLAUDE_AGENT_NAME,
            agent_version=self._agent_version,
            model=self._model,
            adapter_name=self._adapter_name,
            adapter_version=self._adapter_version,
        )

    def capabilities(self) -> AdapterCapabilities:
        """What the 2.1.310 hook surface lets this integration observe.

        ``tool_events`` and ``session_linkage`` are on because both are named in the
        binary's hook vocabulary: the two tool hooks and a stable ``session_id`` on
        every payload.

        Everything else is off, and the important one is
        ``explicit_adoption_signal``: a tool being *used* is not an experience being
        *adopted*, and no Claude Code hook reports the second. Leaving it off means
        the ingestor refuses an adoption claim from this adapter outright, so the
        honest ``UNKNOWN`` cannot be upgraded by accident.
        """
        return AdapterCapabilities(
            tool_events=True,
            session_linkage=True,
            explicit_adoption_signal=False,
            explicit_utility_signal=False,
            human_feedback=False,
            external_verification=False,
        )

    def start(self, raw: Mapping[str, object]) -> AdapterSessionRequest:
        """Translate the payload that establishes a run.

        Normally a ``UserPromptSubmit``, because that is the first Claude Code
        payload that says what was asked; see the module docstring and D-091. A
        payload without a prompt produces a placeholder task rather than an
        exception, so a misordered delivery degrades into a less informative run
        instead of a failed hook.
        """
        session = mapping.session_id(raw)
        prompt = raw.get("prompt")
        task = (
            mapping.task_summary(prompt)
            if isinstance(prompt, str) and prompt.strip()
            else _UNKNOWN_TASK
        )
        # The working directory is the closest thing Claude Code gives to a task
        # category, and it is a fact from the payload rather than a classification
        # this adapter invents.
        cwd = raw.get("cwd")
        return AdapterSessionRequest(
            external_session_id=session,
            task=task,
            task_type=cwd if isinstance(cwd, str) else None,
            metadata=to_json_object(
                {
                    "claude_code": {
                        "cwd": raw.get("cwd"),
                        "transcript_path": raw.get("transcript_path"),
                        "permission_mode": raw.get("permission_mode"),
                        "model": raw.get("model"),
                        "source": raw.get("source"),
                    }
                }
            ),
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        """Translate one hook delivery into zero or more protocol envelopes.

        An empty result is the normal answer for fourteen of the sixteen hooks, and
        it is a decision rather than an oversight: see
        :data:`~aer.adapter.claude.mapping.ENVELOPE_FOR` for the two that are
        mapped, and the coverage matrix for the state of the rest.

        Raises:
            AdapterProtocolError: the payload is not a Claude Code hook delivery.
            UnsupportedAdapterEvent: the event name is outside the version this
                adapter was written for.
        """
        event = mapping.event_name(raw)
        if event not in mapping.ENVELOPE_FOR:
            return ()
        return mapping.tool_envelopes(
            event,
            raw,
            identity=self.identity(),
            common={
                "claude_code": {
                    "event": event.value,
                    "session_id": raw.get("session_id"),
                    "tool_use_id": raw.get("tool_use_id"),
                    "model": raw.get("model"),
                }
            },
        )

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        """Translate a run-closing payload into a finish request.

        The closing events -- ``SessionEnd`` (D-101: the single terminal authority;
        ``Teardown`` is not a hook event at all) and ``UserPromptSubmit`` when it
        ends the previous turn -- never declare a task outcome on this hook surface,
        so every one of them closes the run ``INCONCLUSIVE``:

        * a session ending **is** a run ending. The CLI has torn the conversation
          down (or the user has moved on to a new prompt), so leaving the run
          ``RUNNING`` forever would be the one answer that is certainly wrong;
        * ``INCONCLUSIVE`` says exactly what happened without claiming anything:
          the run is over and no participant declared how it went. It does not
          assert the task failed, and it leaves the run open to independent
          verification -- a passing required check still makes it a verified
          success (round-8.1.1, D-100);
        * closing ``ABORTED`` instead would put every Claude Code run into AER's
          failure vocabulary. ``SessionEnd.reason`` was ``"other"`` on a captured,
          *successful* turn, which is the measured proof that the reason names an
          exit cause, not an outcome (D-099's parallel).

        ``claude_outcome_stated`` records that the payload stated no outcome, so a
        reader can tell "the CLI said nothing" from a hypothetical future payload
        that says something.

        Raises:
            AdapterProtocolError: the payload is not one of the run-closing events,
                so confirming it as a close would be a guess.
        """
        event = mapping.event_name(raw)
        closing_turn = event is mapping.ClaudeHookEvent.USER_PROMPT_SUBMIT
        if not (mapping.closes_run(event) or closing_turn):
            raise AdapterProtocolError(
                f"ClaudeCodeAdapter.finish expects a SessionEnd or turn-ending "
                f"UserPromptSubmit payload, got {event.value}"
            )
        reason = (
            "new prompt submitted; previous turn closed without a declared outcome"
            if closing_turn
            else (str(raw.get("reason")) if raw.get("reason") is not None else None)
        )
        metadata = to_json_object(
            {
                "claude_code": {
                    "event": event.value,
                    "reason": raw.get("reason"),
                    "cwd": raw.get("cwd"),
                    "transcript_path": raw.get("transcript_path"),
                },
                "claude_outcome_stated": False,
            }
        )
        return AdapterFinishRequest(
            status=RunStatus.INCONCLUSIVE,
            reason=reason,
            metadata=metadata,
        )

    def __repr__(self) -> str:
        return (
            f"ClaudeCodeAdapter(version={self._agent_version!r}, "
            f"tested={VERIFIED_CLAUDE_VERSION!r}, mode={ClaudeMode.PRINT.value})"
        )
