"""``RunEvidence`` -- the one thing a distiller is allowed to look at.

The distiller must not wander across repositories picking up whatever it finds
(round-5 brief, section 21). It receives an immutable package that already contains
everything a distillation pass may use, assembled in one place by
:class:`RunEvidenceBuilder`. Two consequences that matter:

* the distiller is storage-agnostic, so it can be tested -- and later swapped for a
  model-backed one -- without a database;
* what a distillation pass saw is a *recorded* fact rather than an accident of query
  order, which is what makes a distilled claim auditable afterwards.

``artifacts`` and ``human_feedback`` are part of the agreed shape (brief section 21).
Human feedback already exists in this milestone as ``HUMAN_FEEDBACK`` events, so it
is exposed as a derived view rather than a second table. Artifacts are **not**
implemented (that is the Artifact milestone) and are deliberately absent rather than
faked as empty.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aer.exceptions import RecordNotFoundError
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.models import (
    ErrorRecord,
    Event,
    RecoveryRecord,
    Run,
    VerificationRecord,
)
from aer.storage.repositories import (
    ErrorRepository,
    EventRepository,
    RecoveryRepository,
    RunRepository,
    VerificationRepository,
)
from aer.verification.summary import VerificationSummary, is_verified_success

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class RunEvidence(BaseModel):
    """Everything known about one run, as an immutable snapshot.

    Frozen and tuple-based so a distillation pass cannot quietly reshape its own
    input: whatever the provider saw is what is recorded alongside the experience.
    """

    model_config = _FROZEN

    run: Run
    events: tuple[Event, ...] = ()
    errors: tuple[ErrorRecord, ...] = ()
    recoveries: tuple[RecoveryRecord, ...] = ()
    verifications: tuple[VerificationRecord, ...] = ()

    # -- identity ----------------------------------------------------------

    @property
    def run_id(self) -> str:
        """Identifier of the run this package describes."""
        return self.run.id

    @property
    def is_finished(self) -> bool:
        """Whether the run reached a terminal status.

        A run that is still going cannot be distilled: its outcome is not a fact yet,
        and an experience built from a half-finished trajectory would describe a
        situation that never existed.
        """
        return self.run.is_finished

    # -- verification views ------------------------------------------------

    @property
    def verification_summary(self) -> VerificationSummary:
        """The verdicts recorded for this run, aggregated (Milestone 4)."""
        return VerificationSummary.from_records(self.run_id, self.verifications)

    @property
    def verified_success(self) -> bool:
        """``agent said SUCCESS`` **and** every required check passed.

        Reuses the Milestone 4 definition rather than restating it, so
        "verified" cannot come to mean two different things in two layers.
        """
        return is_verified_success(self.run.status, self.verification_summary)

    @property
    def required_failed(self) -> int:
        """How many required verifications failed. ``> 0`` means "proven unmet"."""
        return self.verification_summary.required_failed

    # -- failure and repair views ------------------------------------------

    @property
    def has_errors(self) -> bool:
        """Whether any failure was recorded during the run."""
        return bool(self.errors)

    @property
    def error_messages(self) -> tuple[str, ...]:
        """Recorded failures rendered as ``"<error_type>: <message>"``.

        Used as the deterministic source for ``failed_attempts`` when the provider
        does not supply them: the failures were *observed*, so deriving them is
        reporting, not invention.
        """
        rendered: list[str] = []
        for error in self.errors:
            label = error.error_type or "Error"
            rendered.append(f"{label}: {error.error_message}")
        return tuple(rendered)

    @property
    def successful_recoveries(self) -> tuple[RecoveryRecord, ...]:
        """Repair attempts that completed successfully."""
        return tuple(recovery for recovery in self.recoveries if recovery.success is True)

    @property
    def failed_recoveries(self) -> tuple[RecoveryRecord, ...]:
        """Repair attempts that were tried and did not work.

        Distinct from :attr:`errors`: these are *targeted* attempts, which is why
        they are the most useful raw material a failure experience has.
        """
        return tuple(recovery for recovery in self.recoveries if recovery.success is False)

    @property
    def has_successful_recovery(self) -> bool:
        """Whether the run contains a failure that was repaired.

        The definitive signal for a ``RECOVERY`` experience (brief section 18).
        """
        return bool(self.successful_recoveries)

    # -- human input -------------------------------------------------------

    @property
    def human_feedback_events(self) -> tuple[Event, ...]:
        """Every ``HUMAN_FEEDBACK`` observation recorded against this run.

        Human feedback is a Milestone 4 system observation, so it lives in the trace
        rather than in a table of its own.
        """
        return tuple(event for event in self.events if event.event_type is EventType.HUMAN_FEEDBACK)

    @property
    def has_human_feedback(self) -> bool:
        """Whether a human said anything about this run."""
        return bool(self.human_feedback_events)

    # -- trajectory shape --------------------------------------------------

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Tool identifiers called during the run, in order, with repeats kept."""
        names: list[str] = []
        for event in self.events:
            if event.event_type is not EventType.TOOL_CALL:
                continue
            payload = event.input
            if isinstance(payload, dict):
                tool = payload.get("tool")
                if isinstance(tool, str) and tool:
                    names.append(tool)
        return tuple(names)

    @property
    def status(self) -> RunStatus:
        """The agent's own declaration (never a trust signal)."""
        return self.run.status

    def __repr__(self) -> str:
        return (
            f"RunEvidence(run_id={self.run_id!r}, status={self.run.status.value!r}, "
            f"events={len(self.events)}, errors={len(self.errors)}, "
            f"recoveries={len(self.recoveries)}, "
            f"verifications={len(self.verifications)})"
        )


class RunEvidenceBuilder:
    """Assembles a :class:`RunEvidence` from the repositories.

    Exists so the distiller never touches storage and the service never has to know
    which five repository calls a complete picture requires (round-5 brief, section
    22).

    Note what it does *not* do: it does not interpret. It gathers, orders and hands
    over. Every judgement about what the evidence means belongs to the classifier
    and the distiller.
    """

    def __init__(
        self,
        *,
        runs: RunRepository,
        events: EventRepository,
        errors: ErrorRepository,
        recoveries: RecoveryRepository,
        verifications: VerificationRepository,
    ) -> None:
        self._runs = runs
        self._events = events
        self._errors = errors
        self._recoveries = recoveries
        self._verifications = verifications

    def build(self, run_id: str) -> RunEvidence:
        """Load everything about ``run_id``.

        Raises:
            RecordNotFoundError: no such run exists. Silently returning an empty
                package would let a typo masquerade as "a run with no evidence" and
                produce an experience about nothing.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        return self.from_run(run)

    def from_run(self, run: Run) -> RunEvidence:
        """Load the surrounding records for an already-loaded run.

        Events come back ordered by ``sequence`` -- the authoritative ordering key
        (TASKS.md Task 3.3) -- so the assembled package preserves the trace order the
        agent actually produced.
        """
        return RunEvidence(
            run=run,
            events=tuple(self._events.get_by_run(run.id)),
            errors=tuple(self._errors.get_by_run(run.id)),
            recoveries=tuple(self._recoveries.get_by_run(run.id)),
            verifications=tuple(self._verifications.get_by_run(run.id)),
        )
