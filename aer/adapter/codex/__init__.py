"""The Codex adapter: Codex CLI lifecycle hooks, translated into AER evidence.

    Codex hook event  ->  aer.adapter.codex  ->  Agent Adapter Protocol  ->  AER

The core of AER does not know what a Codex hook is (round-8.1 section 3: no
``if provider == "codex"`` anywhere in ``aer/runtime``). This package is where that
knowledge lives, and it lives in three separated pieces:

* :mod:`~aer.adapter.codex.mapping` -- the translation table. Pure functions, payload
  in, envelopes out, so the mapping is testable against captured payloads instead of
  only against a live session;
* :mod:`~aer.adapter.codex.adapter` -- the :class:`~aer.adapter.protocol.AgentAdapter`
  implementation: identity, capabilities, and the one lifecycle decision an adapter
  owns (what a session's ending means for the run);
* :mod:`~aer.adapter.codex.hook` -- the command Codex runs. It reads one payload from
  stdin and routes it, and it is allowed to fail without blocking the agent.

:mod:`~aer.adapter.codex.coverage` carries the part that is easy to skip and expensive
to skip: **what this integration has actually been verified to observe, per mode, for a
named CLI version**. Everything else here is a mapping; that is the evidence for whether
the mapping can be trusted, and it says ``MISSING`` where it has to.

What this adapter deliberately does not do: it does not invent a task for a session that
stated none, does not treat a tool succeeding as an experience being adopted, does not
call a session that ended a task that succeeded, and does not read Codex's rollout files
(section 41). It speaks only to the public hook surface -- no patching of Codex is
involved and none would be (section 43).
"""

from aer.adapter.codex.adapter import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    CodexAdapter,
    detect_codex_version,
)
from aer.adapter.codex.coverage import (
    VERIFIED_CODEX_VERSION,
    VERIFIED_ON,
    CodexHookCoverage,
    CodexMode,
    HookCoverage,
    SessionCompleteness,
    default_coverage,
)
from aer.adapter.codex.mapping import (
    CODEX_AGENT_NAME,
    CODEX_PROVIDER,
    ENVELOPE_FOR,
    CodexHookEvent,
    SessionStartSource,
    event_name,
    external_event_id,
    finish_status,
    session_id,
    task_summary,
    tool_envelopes,
)

__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "CODEX_AGENT_NAME",
    "CODEX_PROVIDER",
    "ENVELOPE_FOR",
    "VERIFIED_CODEX_VERSION",
    "VERIFIED_ON",
    "CodexAdapter",
    "CodexHookCoverage",
    "CodexHookEvent",
    "CodexMode",
    "HookCoverage",
    "SessionCompleteness",
    "SessionStartSource",
    "default_coverage",
    "detect_codex_version",
    "event_name",
    "external_event_id",
    "finish_status",
    "session_id",
    "task_summary",
    "tool_envelopes",
]
