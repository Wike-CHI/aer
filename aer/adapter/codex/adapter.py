"""The Codex adapter: one Agent's vocabulary, translated into the protocol.

    Codex hook payload  ->  this module  ->  AgentExecutionEnvelope  ->  AdapterIngestor

It holds no state and touches no storage. Everything it knows about Codex is a
translation table and a payload field name, and everything it does with them goes
through :mod:`aer.adapter.codex.mapping`. An adapter that remembered something between
deliveries would lose it the moment Codex restarted the process, which is the failure
the persisted session mapping exists to avoid (round-8 sections 9, 21).

Three decisions in here are worth stating, because each one is a refusal to overclaim
about a CLI that was measured rather than assumed:

**The environment identifies the agent, but only up to a point.** ``agent_version`` is
the installed CLI's version when it can be read, and ``"unknown"`` otherwise -- never a
guessed number. ``model`` is recorded only when a payload actually carries it, because
section 4 is explicit that a model name must not be inferred, and section 3 that it must
never be used to decide what kind of agent this is.

**SessionStart does not open the run.** Codex's ``SessionStart`` payload was captured and
it carries no task -- session id, transcript path, cwd, event name, model, permission
mode and a source, and nothing else. A run opened from it would be labelled with a
placeholder for the rest of its life, which is exactly the field AER distils experience
*about*. So the run is established by the first payload that can describe the task, and
``SessionStart`` is recorded as a session-level fact instead (D-091).

**Only two hook events produce envelopes.** ``PreToolUse`` and ``PostToolUse`` have both
an unambiguous AER meaning and a payload shape named by the CLI's own serializer. The
other ten are delivered as nothing and counted as ignored, and the coverage report says
so rather than letting their absence look like a quiet session (D-092).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence

from aer.adapter.codex import mapping
from aer.adapter.codex.coverage import (
    VERIFIED_CODEX_VERSION,
    CodexHookCoverage,
    CodexMode,
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

__all__ = ["ADAPTER_NAME", "ADAPTER_VERSION", "CodexAdapter", "detect_codex_version"]

#: Stable adapter identifier. It is also the ``adapter_name`` recorded on every run, so
#: "which integration produced this?" stays answerable when there are several (round-8
#: section 38).
ADAPTER_NAME = "aer-codex"
#: Version of *this adapter*, not of Codex and not of the protocol.
ADAPTER_VERSION = "1"

#: The task a run gets when a payload can describe a session but not what was asked.
#: Only reachable through :meth:`CodexAdapter.start` being called with a payload that has
#: no prompt, which the hook avoids doing; it exists so the refusal is explicit rather
#: than an exception in the middle of a hook delivery.
_UNKNOWN_TASK = "Codex session (task not stated by any hook payload)"


def detect_codex_version(*, timeout: float = 10.0) -> str:
    """Ask the installed CLI for its version, or report ``"unknown"``.

    Called once when the adapter is built rather than per delivery: a subprocess on the
    hook's synchronous path is exactly the kind of cost section 8 rules out. A failure
    to detect is reported as ``"unknown"`` and never guessed -- an adapter that claims
    ``0.155.1`` because that is what it was written against would make a version
    mismatch invisible in the data.

    ``shutil.which`` is used rather than the bare name because on Windows ``codex`` is
    an npm shim: ``CreateProcess`` performs no PATHEXT lookup, so a bare name raises
    ``FileNotFoundError`` even though the command works in a shell. Measured, not
    assumed -- this returned ``"unknown"`` on a machine where ``codex --version`` plainly
    works.

    Args:
        timeout: Seconds to wait for ``codex --version``.
    """
    resolved = shutil.which("codex") or "codex"
    command = [resolved, "--version"]
    for use_shell in (False, True):
        try:
            completed = subprocess.run(
                "codex --version" if use_shell else command,
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
        if not text:
            continue
        # `codex --version` prints e.g. "codex-cli 0.155.1".
        for token in reversed(text.split()):
            if token and (token[0].isdigit() or token[0] == "v"):
                return token.lstrip("v")
        return text
    return "unknown"


class CodexAdapter:
    """Translates Codex lifecycle hooks into AER protocol envelopes."""

    def __init__(
        self,
        *,
        agent_version: str | None = None,
        model: str | None = None,
        adapter_name: str = ADAPTER_NAME,
        adapter_version: str = ADAPTER_VERSION,
        coverage: CodexHookCoverage | None = None,
        detect_version: bool = True,
    ) -> None:
        self._agent_version = (
            agent_version
            if agent_version is not None
            else (detect_codex_version() if detect_version else "unknown")
        )
        # Only recorded when a caller supplies it, i.e. when a payload actually carried
        # one. Section 4: never inferred.
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
    def coverage(self) -> CodexHookCoverage:
        """What this adapter has been verified to see (sections 37-38)."""
        return self._coverage

    @property
    def tested_codex_versions(self) -> tuple[str, ...]:
        """Codex versions this adapter's mapping was measured against (section 58).

        One entry, not a range: a claim about versions nobody ran is a claim about a
        guess, and the probe exists so that extending this tuple is cheap.
        """
        return (self._coverage.codex_version,)

    def identity(self) -> AgentIdentity:
        """Who this adapter speaks for."""
        return AgentIdentity(
            provider=mapping.CODEX_PROVIDER,
            agent_name=mapping.CODEX_AGENT_NAME,
            agent_version=self._agent_version,
            model=self._model,
            adapter_name=self._adapter_name,
            adapter_version=self._adapter_version,
        )

    def capabilities(self) -> AdapterCapabilities:
        """What Codex 0.155.1 was verified to let an integration observe.

        ``tool_events`` and ``session_linkage`` are on because both were demonstrated:
        tool hooks are named by the CLI's serializer, and every captured payload carried
        a stable ``session_id``.

        Everything else is off, and the important one is
        ``explicit_adoption_signal``. Section 5 is explicit that an Agent *using a tool*
        is not the same as an Agent *adopting an AER experience*, and no Codex hook
        observed here reports the second. Leaving the capability off means the ingestor
        refuses an adoption claim from this adapter outright, so the honest ``UNKNOWN``
        cannot be upgraded by accident.
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

        Normally a ``UserPromptSubmit``, because that is the first Codex payload that
        says what was asked; see the module docstring and D-091. A ``SessionStart``
        payload is accepted and produces a placeholder task rather than an exception, so
        a misordered delivery degrades into a less informative run instead of a failed
        hook.
        """
        session = mapping.session_id(raw)
        prompt = raw.get("prompt")
        task = (
            mapping.task_summary(prompt)
            if isinstance(prompt, str) and prompt.strip()
            else _UNKNOWN_TASK
        )
        # The working directory is the closest thing Codex gives to a task category, and
        # it is a fact from the payload rather than a classification this adapter invents.
        cwd = raw.get("cwd")
        return AdapterSessionRequest(
            external_session_id=session,
            task=task,
            task_type=cwd if isinstance(cwd, str) else None,
            metadata=to_json_object(
                {
                    "codex": {
                        "cwd": raw.get("cwd"),
                        "transcript_path": raw.get("transcript_path"),
                        "permission_mode": raw.get("permission_mode"),
                        "model": raw.get("model"),
                        "source": raw.get("source"),
                        "turn_id": raw.get("turn_id"),
                    }
                }
            ),
        )

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        """Translate one hook delivery into zero or more protocol envelopes.

        An empty result is the normal answer for ten of the twelve hooks, and it is a
        decision rather than an oversight: see :data:`~aer.adapter.codex.mapping.ENVELOPE_FOR`
        for the two that are mapped, and the coverage report for the state of the rest.

        Raises:
            AdapterProtocolError: the payload is not a Codex hook delivery.
            UnsupportedAdapterEvent: the event name is outside the version this adapter
                was written for.
        """
        event = mapping.event_name(raw)
        if event not in mapping.ENVELOPE_FOR:
            return ()
        return mapping.tool_envelopes(
            event,
            raw,
            identity=self.identity(),
            common={
                "codex": {
                    "event": event.value,
                    "session_id": raw.get("session_id"),
                    "turn_id": raw.get("turn_id"),
                    "model": raw.get("model"),
                }
            },
        )

    def finish(self, raw: Mapping[str, object]) -> AdapterFinishRequest:
        """Translate a ``SessionEnd`` payload into a finish request.

        When Codex states an outcome the run takes that status. When it states none --
        which is what was actually observed on 0.155.1, where every captured session
        ended with ``reason: "other"`` -- the run is closed ``INCONCLUSIVE``:

        * a session ending **is** a run ending. Codex has torn the session down, so
          leaving the run ``RUNNING`` forever would be the one answer that is certainly
          wrong: no further signal can arrive;
        * ``INCONCLUSIVE`` is AER's status for exactly this, and it says what happened
          without claiming anything: the run is over and no participant declared how it
          went. It is a statement about the *declaration*, not about the work, so it
          does not assert that the task failed;
        * the useful consequence is that such a run is not a dead end. An independent
          verification can still settle it -- a passing required check makes it a
          verified success -- which is what lets an integration with no outcome field
          produce experience at all (round-8.1.1, D-100). Closing it ``ABORTED``, as an
          earlier revision did, put it in AER's failure vocabulary and made every Codex
          run a failure candidate.

        ``codex_outcome_stated`` records which of the two happened, so a reader can tell
        "Codex said nothing" from "Codex said it was inconclusive".

        Raises:
            AdapterProtocolError: the payload carries no event name, so it cannot be
                confirmed to be a session end at all.
        """
        event = mapping.event_name(raw)
        if event is not mapping.CodexHookEvent.SESSION_END:
            raise AdapterProtocolError(
                f"CodexAdapter.finish expects a SessionEnd payload, got {event.value}"
            )
        stated = mapping.finish_status(raw)
        metadata = to_json_object(
            {
                "codex": {
                    "reason": raw.get("reason"),
                    "cwd": raw.get("cwd"),
                    "transcript_path": raw.get("transcript_path"),
                },
                "codex_outcome_stated": stated is not None,
            }
        )
        return AdapterFinishRequest(
            status=stated if stated is not None else RunStatus.INCONCLUSIVE,
            reason=str(raw.get("reason")) if raw.get("reason") is not None else None,
            metadata=metadata,
        )

    def __repr__(self) -> str:
        return (
            f"CodexAdapter(version={self._agent_version!r}, "
            f"tested={VERIFIED_CODEX_VERSION!r}, mode={CodexMode.EXEC.value})"
        )
