"""AER runtime layer: domain models, enums, lifecycle rules and hooks.

This is the **domain** layer: the objects every other layer speaks in, plus the
deterministic rules that govern them. Two things are deliberately *not* re-exported
here, for the same reason -- the runtime hosts those pipelines, it is not part of
them:

* :class:`~aer.runtime.runtime.AER` -- the facade is a composition root, and it
  imports the application layers in order to wire them together. Re-exporting it
  here would make importing ``aer.runtime.enums`` -- which every layer does --
  drag in the entire application graph, and ``aer.experience`` imports
  ``aer.runtime.enums``, so that is a genuine cycle rather than a style preference.
  Use ``from aer import AER`` (the documented entry point) or
  ``from aer.runtime.runtime import AER``.
* the verification engine (:mod:`aer.verification`) -- it is built *on* the runtime.
"""

from aer.runtime.enums import (
    DistillationTrigger,
    EventType,
    ExperienceKind,
    ExperienceStatus,
    RunStatus,
    VerifierType,
)
from aer.runtime.hooks import RecoveryContext, ToolContext
from aer.runtime.lifecycle import (
    ALLOWED_TRANSITIONS,
    allowed_transitions_from,
    can_transition,
    is_terminal,
    reachable_from,
)
from aer.runtime.models import (
    ErrorRecord,
    Event,
    Experience,
    ExperienceSource,
    RecoveryRecord,
    Run,
    VerificationRecord,
)
from aer.runtime.run import RunContext

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DistillationTrigger",
    "ErrorRecord",
    "Event",
    "EventType",
    "Experience",
    "ExperienceKind",
    "ExperienceSource",
    "ExperienceStatus",
    "RecoveryContext",
    "RecoveryRecord",
    "Run",
    "RunContext",
    "RunStatus",
    "ToolContext",
    "VerificationRecord",
    "VerifierType",
    "allowed_transitions_from",
    "can_transition",
    "is_terminal",
    "reachable_from",
]
