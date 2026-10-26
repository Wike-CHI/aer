"""The Claude Code adapter: Claude Code lifecycle hooks, translated into AER evidence.

    Claude Code hook event  ->  aer.adapter.claude  ->  Agent Adapter Protocol  ->  AER

The core of AER does not know what a Claude Code hook is (round-8.1 section 3: no
``if provider == "claude"`` anywhere in ``aer/runtime``). This package is where that
knowledge lives, and it lives in three separated pieces:

* :mod:`~aer.adapter.claude.mapping` -- the translation table. Pure functions,
  payload in, envelopes out, so the mapping is testable against captured payloads
  instead of only against a live session;
* :mod:`~aer.adapter.claude.adapter` -- the
  :class:`~aer.adapter.protocol.AgentAdapter` implementation: identity,
  capabilities, and the one lifecycle decision an adapter owns (what a session's
  ending means for the run);
* :mod:`~aer.adapter.claude.hook` -- the command Claude Code runs. It reads one
  payload from stdin and routes it, keeping stdout clean because Claude Code parses
  hook stdout as JSON.

:mod:`~aer.adapter.claude.coverage` carries the evidence: the event vocabulary was
extracted from claude.exe 2.1.310's string table, every payload shape starts at
``VOCABULARY``, and ``scripts/probe_claude_hooks.py`` is what upgrades a cell to
``CAPTURED`` with a real fixture.

What this adapter deliberately does not do: it does not invent a task for a session
that stated none, does not treat a tool succeeding as an experience being adopted,
does not call a session that ended a task that succeeded, and does not read
Claude Code's transcript files. It speaks only to the public hook surface -- no
patching is involved and none would be.
"""

from aer.adapter.claude.adapter import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    ClaudeCodeAdapter,
    detect_claude_version,
)
from aer.adapter.claude.coverage import (
    VERIFIED_CLAUDE_VERSION,
    VERIFIED_ON,
    ClaudeHookCoverage,
    ClaudeMode,
    EvidenceLevel,
    HookCoverage,
    SessionCompleteness,
    default_coverage,
)
from aer.adapter.claude.mapping import (
    CLAUDE_AGENT_NAME,
    CLAUDE_PROVIDER,
    ENVELOPE_FOR,
    ClaudeHookEvent,
    closes_run,
    event_name,
    external_event_id,
    session_id,
    task_summary,
    tool_envelopes,
)

__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "CLAUDE_AGENT_NAME",
    "CLAUDE_PROVIDER",
    "ENVELOPE_FOR",
    "VERIFIED_CLAUDE_VERSION",
    "VERIFIED_ON",
    "ClaudeCodeAdapter",
    "ClaudeHookCoverage",
    "ClaudeHookEvent",
    "ClaudeMode",
    "EvidenceLevel",
    "HookCoverage",
    "SessionCompleteness",
    "closes_run",
    "default_coverage",
    "detect_claude_version",
    "event_name",
    "external_event_id",
    "session_id",
    "task_summary",
    "tool_envelopes",
]
