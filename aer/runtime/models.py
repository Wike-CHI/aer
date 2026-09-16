"""AER core domain models (Milestone 1, Task 1.1).

These Pydantic models are the *domain* layer: they are what the runtime, hooks and
future verifiers/distillers exchange. They are deliberately storage-agnostic --
JSON columns, ORM rows and SQL identifiers are a concern of ``aer.storage``.

Design rules applied here:

* every field has an explicit type hint (no ``Any``);
* free-form payloads use the recursive :data:`~aer.runtime.serialization.JsonValue`
  contract instead of bare ``dict``;
* all timestamps are timezone-aware (``pydantic.AwareDatetime``), normalised to
  UTC by :func:`aer.runtime.serialization.utc_now`;
* identifiers are UUID4 strings in this first version;
* ``extra="forbid"`` + ``validate_assignment=True`` so typos and illegal state
  writes fail loudly instead of silently corrupting a trace.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aer.exceptions import ExperienceLifecycleError
from aer.runtime.enums import EventType, ExperienceKind, ExperienceStatus, RunStatus, VerifierType
from aer.runtime.lifecycle import allowed_transitions_from, can_transition, is_terminal
from aer.runtime.serialization import JsonObject, JsonValue, new_id, utc_now

_FORBIDDEN_EXTRA = ConfigDict(extra="forbid", validate_assignment=True)

#: Immutable variant. Used where the object's lifecycle must be owned by an
#: explicit state machine rather than by attribute assignment.
_FROZEN = ConfigDict(extra="forbid", frozen=True)


class Run(BaseModel):
    """One complete agent task execution.

    ``status`` tracks what the *agent runtime* declared. It is intentionally not
    a trust signal: only an independent verifier (Milestone 4) may promote a run
    to a verified success (agent.md #18, TASKS.md Task 4.5).
    """

    model_config = _FORBIDDEN_EXTRA

    task_description: str
    """Human readable description of what the agent was asked to do."""

    id: str = Field(default_factory=new_id)
    task_type: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    model_provider: str | None = None
    model_name: str | None = None

    status: RunStatus = RunStatus.RUNNING

    started_at: AwareDatetime = Field(default_factory=utc_now)
    ended_at: AwareDatetime | None = None

    final_score: float | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def is_finished(self) -> bool:
        """``True`` once the run left the ``RUNNING`` status."""
        return self.status is not RunStatus.RUNNING


class Event(BaseModel):
    """A single standardised event produced during a run.

    ``sequence`` and ``id`` are assigned by the storage layer on first persist,
    so they are ``None`` for an in-memory event that has not been written yet.
    ``sequence`` is the authoritative ordering key inside a run; ``created_at``
    is informational only (TASKS.md Task 3.3).
    """

    model_config = _FORBIDDEN_EXTRA

    run_id: str
    event_type: EventType

    id: int | None = None
    sequence: int | None = Field(default=None, ge=1)

    input: JsonValue | None = None
    output: JsonValue | None = None

    created_at: AwareDatetime = Field(default_factory=utc_now)
    duration_ms: int | None = Field(default=None, ge=0)
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def is_persisted(self) -> bool:
        """``True`` when the event already has a database identity."""
        return self.id is not None


class ErrorRecord(BaseModel):
    """A classified failure captured during a run.

    ``error_type`` stays a plain string on purpose: agent.md #34 wants it derived
    from the underlying failure (``tool``, ``status_code``, ``key_message``, ...),
    i.e. observed data rather than a closed vocabulary. It becomes the basis of
    the error fingerprint in a later milestone.
    """

    model_config = _FORBIDDEN_EXTRA

    run_id: str
    error_message: str

    id: str = Field(default_factory=new_id)
    event_id: int | None = None
    error_type: str | None = None
    stack_trace: str | None = None

    recoverable: bool = True
    resolved: bool = False

    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)


class RecoveryRecord(BaseModel):
    """A failed attempt followed by a repair attempt (Milestone 3).

    Recovery trajectories are the highest-value distillation input
    (agent.md #32), so the links to the originating error and to the two events
    that bracket the attempt are first-class fields rather than metadata.

    ``success`` is ``None`` while the attempt is still open. It is only ever set
    by the runtime that opened it, never inferred from a later successful task --
    agent.md is explicit that ``resolved`` must follow an *explicit* link.
    """

    model_config = _FORBIDDEN_EXTRA

    run_id: str
    reason: str
    """Why the agent decided to recover, e.g. ``"WordPress REST API returned 403"``."""

    id: str = Field(default_factory=new_id)

    error_id: str | None = None
    """The :class:`ErrorRecord` this attempt targets, when the caller supplied one."""

    start_event_id: int | None = None
    result_event_id: int | None = None

    success: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0)

    outcome: JsonValue | None = None
    """Whatever ``recovery.set_result(...)`` was given, if anything."""

    started_at: AwareDatetime = Field(default_factory=utc_now)
    ended_at: AwareDatetime | None = None

    metadata: JsonObject = Field(default_factory=dict)


class VerificationRecord(BaseModel):
    """The verdict of one independent verifier for one run.

    Verification is modelled separately from :class:`Run` so the system can
    represent ``agent says SUCCESS`` while ``verifier says FAILED``
    (TASKS.md Task 4.5). Neither field is derived from the other: ``Run.status``
    records what the *agent runtime* declared, ``passed`` records what an
    independent check *observed*.

    ``passed`` is a real ``bool``. The brief is explicit that the core judgement
    must not be smuggled in as the strings ``"passed"`` / ``"failed"`` -- a
    vocabulary like that invites typos and makes ``if record.passed`` silently
    true for the string ``"failed"``.

    ``required`` marks verdicts that participate in the derived
    ``verified_success`` judgement. An optional verifier (typically an LLM
    quality score) can fail without vetoing an otherwise proven task.

    ``score`` is a *graded quality* signal, not an alternative spelling of
    ``passed``: it stays ``None`` for verifiers that only decide yes/no, and when
    present it is constrained to ``0.0..1.0``. Nothing infers ``passed`` from it.
    """

    model_config = _FORBIDDEN_EXTRA

    run_id: str
    verifier_type: VerifierType
    verifier_name: str
    passed: bool

    id: str = Field(default_factory=new_id)

    #: The ``VERIFICATION`` event that recorded this verdict, when there is one.
    event_id: int | None = None

    required: bool = True

    score: float | None = Field(default=None, ge=0.0, le=1.0)

    message: str | None = None
    """Human readable explanation, redacted and length-capped before storage."""

    result: JsonObject = Field(default_factory=dict)
    """Verifier-specific detail (``{"actual_count": 0, "expected_count": 1}``)."""

    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)


class Experience(BaseModel):
    """One reusable piece of knowledge distilled from one or more runs.

    Deliberately a **single** model with a :class:`~aer.runtime.enums.ExperienceKind`
    discriminator rather than three parallel classes: the storage, the lifecycle and
    the repositories are identical for all kinds, and only the reading of the fields
    differs (round-5 brief, section 5).

    **Frozen.** ``status`` cannot be assigned -- ``experience.status = "PROVEN"``
    raises, and :meth:`transition_to` is the only way to move. The lifecycle is
    deterministic code, so it is enforced by the type rather than by convention
    (section 10).

    Nullability is meaningful, not sloppy:

    * :attr:`root_cause` and :attr:`solution` are optional because an *unresolved*
      failure genuinely has neither. Forcing them would make the distiller invent a
      cause to satisfy the schema, and an invented cause is worse than a missing one
      (sections 6 and 31);
    * :attr:`outcome_verified` says the *outcome* was independently confirmed --
      "this did not work" for a failure, "this worked" for a success. It says nothing
      about whether :attr:`solution` is the true cause (section 33, D-033).
    """

    model_config = _FROZEN

    kind: ExperienceKind
    domain: str

    title: str
    problem: str

    dedup_key: str
    """Normalised (kind, domain, title, problem). Computed by
    :mod:`aer.experience.dedup`, required so it can never be forgotten."""

    id: str = Field(default_factory=new_id)

    symptoms: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    """What was tried and did not work, most valuable on ``RECOVERY`` (section 29)."""

    root_cause: str | None = None
    """A *hypothesis*. Nothing in AER verifies a causal claim yet (D-033)."""

    solution: str | None = None
    """What worked. ``None`` for an unresolved failure."""

    recommended_workflow: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    status: ExperienceStatus = ExperienceStatus.RAW
    """Conservative default: a freshly constructed object has asserted nothing yet."""

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    """Stays ``0.0`` in this milestone. Calibration needs reuse data (D-037)."""

    generalizable: bool = True
    outcome_verified: bool = False

    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    # -- derived views -----------------------------------------------------

    @property
    def allowed_transitions(self) -> frozenset[ExperienceStatus]:
        """Statuses this Experience may move to in one step."""
        return allowed_transitions_from(self.status)

    @property
    def is_terminal(self) -> bool:
        """``True`` once no further transition is possible (i.e. deprecated)."""
        return is_terminal(self.status)

    @property
    def is_deprecated(self) -> bool:
        """``True`` when this knowledge has been withdrawn."""
        return self.status is ExperienceStatus.DEPRECATED

    @property
    def has_verified_solution(self) -> bool:
        """Whether this Experience carries a solution that evidence backs.

        This is the derived answer to the question section 8 warns about. A
        ``FAILURE`` Experience can reach ``status == VERIFIED`` while being the
        opposite of a solution -- "it is confirmed that this did not work" -- so
        ``status`` alone must never be read as "the solution is proven".
        """
        return (
            self.status is ExperienceStatus.VERIFIED
            and self.kind is not ExperienceKind.FAILURE
            and bool(self.solution)
        )

    # -- lifecycle ---------------------------------------------------------

    def can_transition_to(self, status: ExperienceStatus) -> bool:
        """Whether ``status`` is a legal single step from the current one."""
        return can_transition(self.status, status)

    def transition_to(
        self,
        status: ExperienceStatus,
        *,
        reason: str | None = None,
    ) -> Experience:
        """Return a copy of this Experience in ``status``, or raise.

        The returned object is a new instance (the model is frozen) with a bumped
        ``updated_at`` and an audit entry in ``metadata["last_transition"]``.

        Raises:
            ExperienceLifecycleError: the transition is not in the table, or the
                Experience is already in ``status``.
        """
        return self._transition_to(status, reason=reason, changes={})

    def mark_verified(self, *, reason: str | None = None) -> Experience:
        """Move to ``VERIFIED`` and record that the *outcome* is evidence-backed.

        A named operation rather than a bare ``transition_to(VERIFIED)`` because the
        two changes belong together: reaching ``VERIFIED`` is exactly the claim that
        independent evidence supports the outcome, and the flag exists to carry that
        claim for readers who never look at the status.

        For a ``FAILURE`` experience this means "it is verified that this did **not**
        work" -- never "there is a verified solution" (see
        :attr:`has_verified_solution`).

        Raises:
            ExperienceLifecycleError: the transition to ``VERIFIED`` is not legal
                from the current status.
        """
        return self._transition_to(
            ExperienceStatus.VERIFIED,
            reason=reason,
            changes={"outcome_verified": True},
        )

    def _transition_to(
        self,
        status: ExperienceStatus,
        *,
        reason: str | None,
        changes: dict[str, object],
    ) -> Experience:
        """Validate one status change and return the updated copy."""
        target = ExperienceStatus(status)
        if target is self.status:
            raise ExperienceLifecycleError(f"Experience {self.id} is already {target.value}")
        if not can_transition(self.status, target):
            allowed = ", ".join(
                sorted(member.value for member in allowed_transitions_from(self.status))
            )
            raise ExperienceLifecycleError(
                f"{self.status.value} -> {target.value} is not an allowed Experience "
                f"transition (allowed from {self.status.value}: {allowed or 'nothing'})"
            )

        now = utc_now()
        metadata: JsonObject = {
            **self.metadata,
            "last_transition": {
                "from": self.status.value,
                "to": target.value,
                "reason": reason,
                "at": now.isoformat(),
            },
        }
        return self._replace(status=target, updated_at=now, metadata=metadata, **changes)

    def _replace(self, **changes: object) -> Experience:
        """Build a re-validated copy with ``changes`` applied.

        Goes through the constructor rather than ``model_copy(update=...)`` so the
        new object is validated like any other -- ``model_copy`` skips validators,
        which would quietly punch a hole in a frozen model.
        """
        data: dict[str, object] = dict(self.model_dump())
        data.update(changes)
        return Experience(**data)  # type: ignore[arg-type]


class ExperienceSource(BaseModel):
    """The link between an Experience and one Run that supports it.

    Its own entity rather than a column on :class:`Experience` so that one
    experience can accumulate evidence::

        Experience #38 (WordPress REST API 403)
            <- run_001
            <- run_017
            <- run_083

    Repeated occurrences merge into this table instead of spawning near-duplicate
    experiences (agent.md #25, round-5 brief sections 12-13). The storage layer
    enforces ``UNIQUE(experience_id, run_id)``, so the same run can never be counted
    twice -- which is also what makes distillation idempotent (section 42).
    """

    model_config = _FROZEN

    experience_id: str
    run_id: str
    created_at: AwareDatetime = Field(default_factory=utc_now)
