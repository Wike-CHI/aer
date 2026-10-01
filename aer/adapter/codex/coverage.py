"""What this Codex integration has actually been verified to see.

The whole point of this module is to make a gap *legible*. An adapter that quietly
records whatever it happens to receive, and calls the result a trace, produces data
whose missing half is invisible: a run with no tool events looks exactly like a run
that used no tools. Section 37 asks for a coverage report for exactly that reason, and
section 38 asks the adapter to declare itself degraded rather than claim a full trace.

So coverage is data, not prose:

* :class:`CodexMode` -- the two ways Codex actually runs, which dispatch hooks
  independently. Section 36 is emphatic that "interactive works" is not evidence about
  ``exec``; a coverage claim is therefore always per-mode;
* :class:`EvidenceLevel` -- how strong the evidence behind one cell is. Three levels, not
  a boolean, because "we captured a real payload and checked the mapping against it" and
  "we wrote a mapping for a shape we have never seen" are different claims (section 19);
* :class:`HookCoverage` -- one event's status in one mode;
* :class:`CodexHookCoverage` -- the matrix, plus the conclusion it supports.

Everything here was established by running the probe against the installed CLI, and the
:data:`VERIFIED_*` values below carry the date and the version they came from. Re-running
the probe is what keeps them honest (section 34).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "VERIFIED_CODEX_VERSION",
    "VERIFIED_ON",
    "CodexHookCoverage",
    "CodexMode",
    "EvidenceLevel",
    "HookCoverage",
    "SessionCompleteness",
    "default_coverage",
]

#: The Codex CLI version every claim in this file was measured against.
VERIFIED_CODEX_VERSION = "0.155.1"
#: When it was measured.
VERIFIED_ON = "2026-09-20"


class CodexMode(StrEnum):
    """How Codex is being driven.

    Not a cosmetic distinction: hook dispatch is a property of the entry point, so one
    mode's coverage says nothing about the other's (section 36).
    """

    INTERACTIVE = "interactive"
    """The TUI. Needs a terminal, so it is not covered by the automated probe."""

    EXEC = "exec"
    """``codex exec``, the non-interactive entry point."""


class EvidenceLevel(StrEnum):
    """How strong the evidence behind one coverage cell is.

    A boolean would erase the distinction that matters most in this file. "The mapping
    exists and has never met a real payload" and "a real payload was captured and the
    mapping was checked against it" are different claims, and only the second one
    supports saying an integration works (sections 19 and 20).

    ``CONTRACT_ONLY`` is the honest middle: code that tolerates a shape taken from the
    CLI's own serializer, with nothing captured to confirm the shape is real. Having it
    is better than having no tool mapping at all -- but it must never be reported as
    observed, because a test driving a hand-written payload proves the code handles that
    payload and says nothing about what Codex sends.
    """

    CAPTURED = "CAPTURED"
    """A real payload was captured from this CLI and the mapping was checked against it."""

    CONTRACT_ONLY = "CONTRACT_ONLY"
    """The mapping exists for an unobserved shape; no captured payload backs it."""

    MISSING = "MISSING"
    """Nothing was observed firing, and nothing is mapped."""


class SessionCompleteness(StrEnum):
    """How much of a session's activity AER can claim to have recorded.

    Section 39 asks for this as a derived state. It is deliberately not a schema
    column: it is a function of coverage and of what was actually delivered, and a
    stored copy would go stale the moment coverage changed.
    """

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
        """Whether a real payload was captured for this event in this mode.

        Deliberately narrow: ``CONTRACT_ONLY`` answers ``False``. Code that has never met
        a real event has not been tested by one, whatever its test suite says.
        """
        return self.level is EvidenceLevel.CAPTURED

    @property
    def label(self) -> str:
        """The short form for a matrix printout."""
        return self.level.value


@dataclass(frozen=True, slots=True)
class CodexHookCoverage:
    """The coverage matrix, and the claims it does and does not support."""

    codex_version: str
    verified_on: str
    modes: dict[CodexMode, dict[str, HookCoverage]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def observed(self, mode: CodexMode, event: str) -> bool:
        """Whether a real payload was captured for ``event`` in ``mode``."""
        return self._cell(mode, event).observed

    def level(self, mode: CodexMode, event: str) -> EvidenceLevel:
        """The evidence level behind one cell."""
        return self._cell(mode, event).level

    def missing(self, mode: CodexMode) -> tuple[str, ...]:
        """Events with no captured payload in ``mode``, in declaration order.

        Includes ``CONTRACT_ONLY``: a mapping nobody has seen a real event for is still a
        gap, and reporting it as covered is the failure this module exists to prevent.
        """
        return tuple(event for event, cell in self.modes.get(mode, {}).items() if not cell.observed)

    def contract_only(self, mode: CodexMode) -> tuple[str, ...]:
        """Events whose mapping exists but has never met a real payload."""
        return tuple(
            event
            for event, cell in self.modes.get(mode, {}).items()
            if cell.level is EvidenceLevel.CONTRACT_ONLY
        )

    def completeness(self, mode: CodexMode, *, fired: tuple[str, ...]) -> SessionCompleteness:
        """Judge one session from the hooks that actually fired during it.

        ``fired`` is what the integration saw for *this* session, not what the mode can
        theoretically do -- a session where the agent never called a tool is complete
        without ``PreToolUse``, and claiming otherwise would turn a quiet task into a
        suspected outage.
        """
        seen = set(fired)
        if not seen:
            return SessionCompleteness.UNKNOWN
        expected = self._expected(mode)
        if seen.issuperset(expected):
            return SessionCompleteness.FULL
        if seen & expected:
            # Something arrived that this mode is known to deliver, and not everything.
            # That is the definition of a partial trace, and it is worth surfacing rather
            # than reporting as "unknown": a session whose start was seen is exactly the
            # session an operator will ask about.
            return SessionCompleteness.PARTIAL
        return SessionCompleteness.UNKNOWN

    def describe(self, mode: CodexMode) -> str:
        """A printable matrix for one mode, with the evidence level of every cell."""
        lines = [f"Codex {self.codex_version} ({mode.value} mode), verified {self.verified_on}"]
        for event, cell in self.modes.get(mode, {}).items():
            suffix = f"  # {cell.note}" if cell.note else ""
            lines.append(f"  {event:18} {cell.label:14}{suffix}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)

    def _cell(self, mode: CodexMode, event: str) -> HookCoverage:
        fallback = HookCoverage(event, EvidenceLevel.MISSING)
        return self.modes.get(mode, {}).get(event, fallback)

    def _expected(self, mode: CodexMode) -> frozenset[str]:
        """The events that *should* appear in a normal session of this mode."""
        if mode is CodexMode.EXEC:
            return frozenset({"SessionStart", "UserPromptSubmit", "SessionEnd"})
        return frozenset(self.modes.get(mode, {}))


def default_coverage() -> CodexHookCoverage:
    """The measured coverage for :data:`VERIFIED_CODEX_VERSION`.

    Read the levels rather than counting them. Three hooks are ``CAPTURED``; the two tool
    hooks are ``CONTRACT_ONLY`` -- a working mapping written against the CLI's own
    serializer, with no captured payload behind it; everything else is ``MISSING``.

    Five hooks are ``CAPTURED``: the three session hooks plus ``PreToolUse`` and
    ``PostToolUse``. ``Stop`` is ``CAPTURED`` too. The rest remain ``MISSING`` because
    nothing was observed firing them -- compaction, subagents, permission requests and
    interrupts all need a session shape these probes did not produce.

    The capture that changed the tool cells also **contradicted** what the earlier
    contract fixtures assumed: ``tool_response`` is a plain string with no outcome field,
    so a failed tool writes no ``ERROR`` (D-097). That is the whole argument for the
    ``CONTRACT_ONLY`` level existing -- those two cells were reported honestly and then
    turned out to be wrong in a way that mattered.
    """
    exec_matrix: dict[str, HookCoverage] = {
        "SessionStart": HookCoverage("SessionStart", EvidenceLevel.CAPTURED, "payload captured"),
        "UserPromptSubmit": HookCoverage(
            "UserPromptSubmit", EvidenceLevel.CAPTURED, "payload captured"
        ),
        "SessionEnd": HookCoverage("SessionEnd", EvidenceLevel.CAPTURED, "payload captured"),
        "PreToolUse": HookCoverage("PreToolUse", EvidenceLevel.CAPTURED, "payload captured"),
        "PostToolUse": HookCoverage(
            "PostToolUse", EvidenceLevel.CAPTURED, "payload captured (string response, no outcome)"
        ),
        "Stop": HookCoverage(
            "Stop", EvidenceLevel.CAPTURED, "payload captured; turn level, carries no outcome"
        ),
    }
    for event in (
        "PermissionRequest",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
        "SubagentStop",
        "Interrupt",
    ):
        exec_matrix[event] = HookCoverage(
            event, EvidenceLevel.MISSING, "not produced by the probed session shape"
        )

    interactive = {
        event: HookCoverage(
            event, EvidenceLevel.MISSING, "needs a terminal; not covered by the probe"
        )
        for event in (
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "PreCompact",
            "PostCompact",
            "SubagentStart",
            "SubagentStop",
            "Stop",
            "SessionEnd",
            "Interrupt",
        )
    }
    return CodexHookCoverage(
        codex_version=VERIFIED_CODEX_VERSION,
        verified_on=VERIFIED_ON,
        modes={
            CodexMode.EXEC: exec_matrix,
            CodexMode.INTERACTIVE: interactive,
        },
        notes=(
            "Stop did not fire in a session whose turn never completed; its relationship "
            "to SessionEnd is therefore unverified and it is not used as a terminal signal",
            "a failing hook is reported as Failed and is NOT retried",
            "SessionEnd and Interrupt timeouts are clamped to 3 seconds",
            "the hook program path must be unquoted; a quoted program silently fails to launch",
            "PostToolUse carries tool_response as a plain string: there is no outcome "
            "field, so a failing tool produces no ERROR event (D-097)",
            "Stop is turn level and carries no outcome, so it still does not terminate a "
            "run (D-098)",
            "SessionEnd.reason was 'other' on a successful turn, so 'other' is normal (D-099)",
        ),
    )
