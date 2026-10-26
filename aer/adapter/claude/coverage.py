"""What this Claude Code integration has actually been verified to see.

Same contract as :mod:`aer.adapter.codex.coverage`: a gap must be *legible*. A run
with no tool events looks exactly like a run that used no tools, so the matrix
says per event, per mode, how strong the evidence behind its mapping is.

One evidence level distinguishes this matrix from the Codex one at birth. The
Codex adapter reached ``CAPTURED`` by running its probe before delivery; this
adapter ships with the event *vocabulary* extracted from claude.exe 2.1.310's
string table (event names and payload keys are literals in the binary -- that is
captured), while every payload *shape* starts at ``CONTRACT_ONLY`` until
``scripts/probe_claude_hooks.py`` captures a real delivery and the cells are
upgraded with fixtures to prove it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from aer.adapter.claude.mapping import ClaudeHookEvent

__all__ = [
    "VERIFIED_CLAUDE_VERSION",
    "VERIFIED_ON",
    "ClaudeHookCoverage",
    "ClaudeMode",
    "EvidenceLevel",
    "HookCoverage",
    "SessionCompleteness",
    "default_coverage",
]

#: The Claude Code version the vocabulary below was extracted from.
VERIFIED_CLAUDE_VERSION = "2.1.310"
#: When it was measured.
VERIFIED_ON = "2026-10-26"


class ClaudeMode(StrEnum):
    """How Claude Code is being driven.

    ``-p``/print sessions are what the automated probe can drive; the interactive
    TUI needs a terminal and is covered separately, for the same reason the Codex
    matrix keeps its two modes apart.
    """

    PRINT = "print"
    """``claude -p``, the non-interactive entry point."""

    INTERACTIVE = "interactive"
    """The TUI. Needs a terminal, so it is not covered by the automated probe."""


class EvidenceLevel(StrEnum):
    """How strong the evidence behind one coverage cell is.

    ``VOCABULARY`` is the level this matrix introduces: the event name and its
    payload *keys* are literals extracted from the installed binary, but no payload
    has been captured, so the shape the mapping tolerates is unconfirmed. It ranks
    above ``CONTRACT_ONLY`` (nothing in the vendor's own artifacts names the shape)
    and below ``CAPTURED`` (a real payload was checked against the mapping).
    """

    CAPTURED = "CAPTURED"
    """A real payload was captured from this CLI and the mapping checked against it."""

    VOCABULARY = "VOCABULARY"
    """Event name and payload keys are literals in the installed binary; no payload captured."""

    CONTRACT_ONLY = "CONTRACT_ONLY"
    """The mapping exists for a shape nothing in the vendor's artifacts names."""

    MISSING = "MISSING"
    """Nothing is known and nothing is mapped."""


class SessionCompleteness(StrEnum):
    """How much of a session's activity AER can claim to have recorded."""

    FULL = "FULL"
    """Every hook the mode supports was observed, and the crucial ones fired."""

    PARTIAL = "PARTIAL"
    """Some hooks fired and some did not -- a usable but incomplete trace."""

    UNKNOWN = "UNKNOWN"
    """Nothing decisive was observed; do not draw conclusions about this session."""


@dataclass(frozen=True, slots=True)
class HookCoverage:
    """One hook event's status in one mode."""

    event: str
    level: EvidenceLevel
    note: str = ""

    @property
    def observed(self) -> bool:
        """Whether a real payload was captured for this event in this mode."""
        return self.level is EvidenceLevel.CAPTURED

    @property
    def label(self) -> str:
        """The short form for a matrix printout."""
        return self.level.value


@dataclass(frozen=True, slots=True)
class ClaudeHookCoverage:
    """The coverage matrix, and the claims it does and does not support."""

    claude_version: str
    verified_on: str
    modes: dict[ClaudeMode, dict[str, HookCoverage]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def observed(self, mode: ClaudeMode, event: str) -> bool:
        """Whether a real payload was captured for ``event`` in ``mode``."""
        return self._cell(mode, event).observed

    def level(self, mode: ClaudeMode, event: str) -> EvidenceLevel:
        """The evidence level behind one cell."""
        return self._cell(mode, event).level

    def missing(self, mode: ClaudeMode) -> tuple[str, ...]:
        """Events with no captured payload in ``mode``, in declaration order."""
        return tuple(event for event, cell in self.modes.get(mode, {}).items() if not cell.observed)

    def contract_only(self, mode: ClaudeMode) -> tuple[str, ...]:
        """Events whose mapping exists but has never met a real payload."""
        return tuple(
            event
            for event, cell in self.modes.get(mode, {}).items()
            if cell.level in (EvidenceLevel.CONTRACT_ONLY, EvidenceLevel.VOCABULARY)
        )

    def completeness(self, mode: ClaudeMode, *, fired: tuple[str, ...]) -> SessionCompleteness:
        """Judge one session from the hooks that actually fired during it."""
        seen = set(fired)
        if not seen:
            return SessionCompleteness.UNKNOWN
        expected = self._expected(mode)
        if seen.issuperset(expected):
            return SessionCompleteness.FULL
        if seen & expected:
            return SessionCompleteness.PARTIAL
        return SessionCompleteness.UNKNOWN

    def describe(self, mode: ClaudeMode) -> str:
        """A printable matrix for one mode, with the evidence level of every cell."""
        lines = [
            f"Claude Code {self.claude_version} ({mode.value} mode), verified {self.verified_on}"
        ]
        for event, cell in self.modes.get(mode, {}).items():
            suffix = f"  # {cell.note}" if cell.note else ""
            lines.append(f"  {event:20} {cell.label:14}{suffix}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)

    def _cell(self, mode: ClaudeMode, event: str) -> HookCoverage:
        fallback = HookCoverage(event, EvidenceLevel.MISSING)
        return self.modes.get(mode, {}).get(event, fallback)

    def _expected(self, mode: ClaudeMode) -> frozenset[str]:
        """The events that *should* appear in a normal session of this mode."""
        if mode is ClaudeMode.PRINT:
            return frozenset({"SessionStart", "UserPromptSubmit", "Stop"})
        return frozenset(self.modes.get(mode, {}))


def default_coverage() -> ClaudeHookCoverage:
    """The measured coverage for :data:`VERIFIED_CLAUDE_VERSION`.

    Nine cells are ``CAPTURED``: real payloads from ``scripts/probe_claude_hooks.py``
    (2026-10-26, print mode), stored verbatim under ``tests/adapters/claude/fixtures/``.
    The rest are ``VOCABULARY``: event names the CLI's own validator confirmed when
    it rejected two names this adapter originally offered (``Teardown`` and
    ``PostToolUseBatch`` -- the string table had named both, the validator named
    neither; the validator is the authority, recorded as D-101). Upgrading a
    ``VOCABULARY`` cell means capturing a payload and adding a fixture, the same
    discipline the Codex matrix keeps.
    """
    captured: dict[str, str] = {
        "SessionStart": "payload captured (no task field; D-091 confirmed by capture)",
        "UserPromptSubmit": "payload captured (prompt + prompt_id correlation)",
        "PreToolUse": "payload captured (tool_input structured, tool_use_id present)",
        "PostToolUse": "payload captured (tool_response has NO outcome field; D-102)",
        "PostToolUseFailure": "payload captured (structured error; the failure channel, D-102)",
        "Stop": "payload captured; fired on a failed provider call too -- turn-level, no outcome",
        "SessionEnd": "payload captured (reason='other' on a successful turn; D-099 parallel)",
        "InstructionsLoaded": "payload captured (memory file loads; not mapped to AER events)",
        "CwdChanged": "payload captured (old/new cwd; an environment fact, not mapped)",
    }
    vocabulary = "event name confirmed by the CLI validator; payload shape not captured yet"
    print_matrix: dict[str, HookCoverage] = {}
    for event in ClaudeHookEvent:
        if event.value in captured:
            print_matrix[event.value] = HookCoverage(
                event.value, EvidenceLevel.CAPTURED, captured[event.value]
            )
        else:
            print_matrix[event.value] = HookCoverage(
                event.value, EvidenceLevel.VOCABULARY, vocabulary
            )
    interactive = {
        event: HookCoverage(
            event, EvidenceLevel.MISSING, "needs a terminal; not covered by the probe"
        )
        for event in print_matrix
    }
    return ClaudeHookCoverage(
        claude_version=VERIFIED_CLAUDE_VERSION,
        verified_on=VERIFIED_ON,
        modes={ClaudeMode.PRINT: print_matrix, ClaudeMode.INTERACTIVE: interactive},
        notes=(
            "event vocabulary confirmed by the CLI's own validator message, not the "
            "string table: 'Teardown' and 'PostToolUseBatch' were rejected (D-101)",
            "SessionEnd is the single terminal authority; its reason is an exit cause, "
            "'other' appeared on a successful turn, so no reason reads as an outcome "
            "(D-099 parallel)",
            "PostToolUse carries no failure signal in tool_response; failures arrive "
            "as the dedicated PostToolUseFailure event with a structured error (D-102)",
            "Stop is turn-level (last_assistant_message, no outcome) and fired on a "
            "failed provider call too, so it never terminates a run (D-098 parallel)",
            "UserPromptSubmit closes the previous run when one is still open: a "
            "session outlives its tasks, an AER run must not",
            "keep the hook fast, and keep stdout clean -- Claude Code parses hook stdout as JSON",
        ),
    )
