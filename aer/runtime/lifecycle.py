"""The Experience lifecycle: which status changes are legal, and in what order.

Everything about *when* an Experience may move between statuses lives here, so the
rule is stated once and can be read, tested and quoted. ``Experience.transition_to``
is the only caller.

The order is **RAW -> DISTILLED -> VERIFIED** (``docs/DECISIONS.md`` D-029):

* ``RAW``       -- a candidate is persisted but has not been through distillation;
* ``DISTILLED`` -- the trajectory has been compressed into a structured claim;
* ``VERIFIED``  -- the core facts of that claim are backed by external evidence.

``agent.md #24``, ``docs/TASKS.md`` Task 5.2 and the design document originally
declared ``RAW -> VERIFIED -> DISTILLED``. That ordering is logically impossible in
this architecture: verification is a check *of a claim*, and a claim only exists
after distillation, so "verified before distilled" would mean verifying something
that had not been formulated yet. The three documents were corrected in this
milestone rather than implementing the old order mechanically.

Only the first three statuses can be reached today. ``REUSED`` and beyond need data
that does not exist yet (reuse tracking arrives with ``experience_usage`` in
Milestone 7), so this table deliberately contains **no edges into them**: an
Experience cannot claim to have been reused when nothing has recorded a reuse
(section 56 of the round-5 brief forbids exactly that promotion). The edges out of
them exist so invalidation keeps working the moment they become reachable.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from aer.runtime.enums import ExperienceStatus

#: Legal transitions, keyed by current status.
#:
#: Read as "from X you may go to any of these". An empty set means the status is
#: terminal. Every non-terminal status may be ``DEPRECATED``: withdrawing
#: knowledge has to stay possible regardless of how far the knowledge got.
ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        ExperienceStatus.RAW: frozenset({ExperienceStatus.DISTILLED, ExperienceStatus.DEPRECATED}),
        ExperienceStatus.DISTILLED: frozenset(
            {ExperienceStatus.VERIFIED, ExperienceStatus.DEPRECATED}
        ),
        ExperienceStatus.VERIFIED: frozenset({ExperienceStatus.DEPRECATED}),
        # Reachable only once reuse tracking exists (Milestone 7).
        ExperienceStatus.REUSED: frozenset({ExperienceStatus.DEPRECATED}),
        ExperienceStatus.PROVEN: frozenset({ExperienceStatus.DEPRECATED}),
        ExperienceStatus.TRAINING_CANDIDATE: frozenset({ExperienceStatus.DEPRECATED}),
        ExperienceStatus.TRAINING_DATA: frozenset({ExperienceStatus.DEPRECATED}),
        # Terminal: an invalidated Experience never comes back to life.
        ExperienceStatus.DEPRECATED: frozenset(),
    }
)


def allowed_transitions_from(status: ExperienceStatus) -> frozenset[ExperienceStatus]:
    """Statuses reachable from ``status`` in one step (empty when terminal)."""
    return ALLOWED_TRANSITIONS[ExperienceStatus(status)]


def can_transition(current: ExperienceStatus, target: ExperienceStatus) -> bool:
    """Whether ``current -> target`` is a legal single step.

    A no-op transition is not "legal but unnecessary": it is rejected, because a
    caller asking to move to the status it is already in has misunderstood
    something, and swallowing that turns a bug into silence.
    """
    current = ExperienceStatus(current)
    target = ExperienceStatus(target)
    return target is not current and target in ALLOWED_TRANSITIONS[current]


def is_terminal(status: ExperienceStatus) -> bool:
    """Whether ``status`` has no outgoing transitions."""
    return not ALLOWED_TRANSITIONS[ExperienceStatus(status)]


def reachable_from(status: ExperienceStatus) -> frozenset[ExperienceStatus]:
    """Every status reachable from ``status`` by following legal transitions."""
    seen: set[ExperienceStatus] = set()
    frontier = [ExperienceStatus(status)]
    while frontier:
        current = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)
