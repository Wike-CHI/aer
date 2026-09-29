"""The Experience lifecycle: which status changes are legal, and in what order.

Everything about *when* an Experience may move between statuses lives here, so the
rule is stated once and can be read, tested and quoted. ``Experience.transition_to``
is the only caller.

The order is **RAW -> DISTILLED -> VERIFIED -> REUSED -> PROVEN** (``docs/DECISIONS.md``
D-029, extended in Milestone 7):

* ``RAW``       -- a candidate is persisted but has not been through distillation;
* ``DISTILLED`` -- the trajectory has been compressed into a structured claim;
* ``VERIFIED``  -- the core facts of that claim are backed by external evidence;
* ``REUSED``    -- it was injected into a real run other than the one it came from;
* ``PROVEN``    -- it was explicitly adopted in several distinct, verified-successful
  runs and has no harmful feedback against it.

``agent.md #24``, ``docs/TASKS.md`` Task 5.2 and the design document originally
declared ``RAW -> VERIFIED -> DISTILLED``. That ordering is logically impossible in
this architecture: verification is a check *of a claim*, and a claim only exists
after distillation, so "verified before distilled" would mean verifying something
that had not been formulated yet. The three documents were corrected in this
milestone rather than implementing the old order mechanically.

Only ``RAW`` through ``PROVEN`` can be reached today. ``TRAINING_CANDIDATE`` and
``TRAINING_DATA`` still have no producer -- turning knowledge into training data needs
a dataset builder and a quality gate, which are not part of this milestone -- so this
table deliberately contains **no edges into them**. What changed in Milestone 7 is the
span from ``VERIFIED`` to ``PROVEN``: those two edges are now real, because usage
tracking finally supplies the evidence they require.

* ``VERIFIED -> REUSED`` -- the claim has been verified *and* has been injected into a
  real run other than the one it was distilled from. Merely being retrieved does not
  count (round-7 brief, sections 33-34), and a successful outcome is not required
  (section 35): being used again is the fact, not being used well;
* ``REUSED -> PROVEN`` -- a stricter question, answered by
  :mod:`aer.usage.promotion`, not by this table. The edge exists here so the state
  machine permits it; the policy decides when.

Every non-terminal status may still be ``DEPRECATED``: withdrawing knowledge has to
stay possible regardless of how far it got.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from aer.runtime.enums import ExperienceStatus

#: Legal transitions, keyed by current status.
#:
#: Read as "from X you may go to any of these". An empty set means the status is
#: terminal.
ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        ExperienceStatus.RAW: frozenset({ExperienceStatus.DISTILLED, ExperienceStatus.DEPRECATED}),
        ExperienceStatus.DISTILLED: frozenset(
            {ExperienceStatus.VERIFIED, ExperienceStatus.DEPRECATED}
        ),
        ExperienceStatus.VERIFIED: frozenset(
            {ExperienceStatus.REUSED, ExperienceStatus.DEPRECATED}
        ),
        # Promotable further, and withdrawable. Both directions are decided by data.
        ExperienceStatus.REUSED: frozenset({ExperienceStatus.PROVEN, ExperienceStatus.DEPRECATED}),
        # No edge into TRAINING_CANDIDATE yet: that needs a dataset builder and a
        # quality gate, and an experience that is merely proven is not yet training
        # material (round-7 brief, section 81).
        ExperienceStatus.PROVEN: frozenset({ExperienceStatus.DEPRECATED}),
        ExperienceStatus.TRAINING_CANDIDATE: frozenset({ExperienceStatus.DEPRECATED}),
        ExperienceStatus.TRAINING_DATA: frozenset({ExperienceStatus.DEPRECATED}),
        # Terminal: an invalidated Experience never comes back to life.
        ExperienceStatus.DEPRECATED: frozenset(),
    }
)

#: Lifecycle order, without the terminal withdrawal state.
#:
#: ``DEPRECATED`` is declared last in :class:`ExperienceStatus` but is not the *most*
#: advanced state -- it is the withdrawn one -- so it is excluded from the ordering
#: rather than accidentally ranked above ``TRAINING_DATA``.
_ORDERED_STATUSES: Final = tuple(
    status for status in ExperienceStatus if status is not ExperienceStatus.DEPRECATED
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


def status_rank(status: ExperienceStatus) -> int:
    """Position of ``status`` in the lifecycle order.

    ``DEPRECATED`` has no rank and reports ``-1``: a withdrawn experience is not
    "past the end" of the lifecycle, it is outside it, and every comparison that
    matters in Milestone 7 (promotion policy) must treat it as ineligible rather
    than as maximally advanced.
    """
    status = ExperienceStatus(status)
    if status is ExperienceStatus.DEPRECATED:
        return -1
    return _ORDERED_STATUSES.index(status)


def is_at_least(status: ExperienceStatus, floor: ExperienceStatus) -> bool:
    """Whether ``status`` is at or beyond ``floor`` in lifecycle order.

    Used by the promotion policy ("status must be at least ``REUSED``"), which is why
    a deprecated experience answers ``False`` for every floor.
    """
    return status_rank(status) >= status_rank(floor)


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
