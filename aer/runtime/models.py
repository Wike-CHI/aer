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

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aer.exceptions import ExperienceLifecycleError, UsageTrackingError
from aer.runtime.enums import (
    EventType,
    ExperienceKind,
    ExperienceStatus,
    RetrievalMode,
    RunStatus,
    SessionAssignment,
    UsageRole,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
    VerifierType,
)
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


class RetrievalSession(BaseModel):
    """One retrieval event: "at this time, for this run, we searched for this".

    The fact this model exists to make storable is negative as often as positive.
    A session with ``result_count == 0`` is a real, useful fact, because "we looked
    for experience about this and there was none" and "nobody ever looked" are
    different states of the world and only one of them is a knowledge-base gap
    (round-7 brief, section 7).

    Three fields are worth explaining:

    * ``query_text`` is the **sanitised** question, never the raw prompt, tool
      output or conversation it was derived from (section 6). It is a record of what
      was asked, not a copy of the caller's context;
    * ``query_fingerprint`` groups the same question asked repeatedly, so "is this
      problem recurring" is answerable without text matching;
    * ``requested_limit`` is the retrieval budget that was actually applied, after
      clamping to the allowed range. Storing the un-clamped request would record a
      sizing mistake as a fact (section 5).

    ``run_id`` is nullable because a retrieval may happen *before* the run it will
    inform exists (section 24); :meth:`attach_run` is how a late arrival is linked.
    ``experiment_id`` / ``assignment`` are reserved for the holdout design of
    section 32 and carry no behaviour today.
    """

    model_config = _FROZEN

    query_text: str
    query_fingerprint: str

    id: str = Field(default_factory=new_id)

    run_id: str | None = None
    domain: str | None = None

    mode: RetrievalMode = RetrievalMode.GUIDANCE
    requested_limit: int = Field(ge=1)
    result_count: int = Field(ge=0)

    knowledge_projection_version: int | None = None
    retrieval_policy_version: str = ""

    retrieval_duration_ms: int = Field(default=0, ge=0)

    experiment_id: str | None = None
    assignment: SessionAssignment = SessionAssignment.NONE

    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def found_nothing(self) -> bool:
        """Whether the search ran successfully and returned nothing.

        Distinct from "the index was unreachable", which raises rather than
        producing a session at all.
        """
        return self.result_count == 0

    @property
    def is_attached(self) -> bool:
        """Whether this retrieval has been linked to a run."""
        return self.run_id is not None

    def attach_run(self, run_id: str, *, reason: str | None = None) -> RetrievalSession:
        """Return a copy linked to ``run_id``.

        Raises:
            UsageTrackingError: this session is already attached to a *different*
                run. Re-pointing a retrieval at another task would silently
                reattribute every usage row beneath it, which is exactly the kind of
                retroactive rewrite this milestone forbids.
        """
        if self.run_id is not None and self.run_id != run_id:
            raise UsageTrackingError(
                f"Retrieval session {self.id} is already attached to run {self.run_id}; "
                f"refusing to re-point it at {run_id}"
            )
        if self.run_id == run_id:
            return self
        return self._replace(
            run_id=run_id,
            metadata=_with_audit(self.metadata, "attach_run", run_id, reason),
        )

    def _replace(self, **changes: object) -> RetrievalSession:
        """Build a re-validated copy with ``changes`` applied."""
        data: dict[str, object] = dict(self.model_dump())
        data.update(changes)
        return RetrievalSession(**data)  # type: ignore[arg-type]


class ExperienceUsage(BaseModel):
    """One experience, as it was offered to one retrieval session (section 8).

    One row per ``(session, experience)`` pair -- enforced by a database UNIQUE
    constraint, because the same experience appearing twice in one result is a bug,
    not a quantity (section 49).

    The row records two independent histories, and keeping them independent is the
    point:

    * **exposure** -- was it retrieved, and was it actually placed in the agent's
      context (``retrieved_at`` / ``injected_at``)? A retrieval result that was never
      rendered is not use (sections 2 and 17);
    * **judgement** -- did anyone *say* the agent used it (``usage_signal``), and did
      anyone *say* it helped (``utility_label``)? Both default to ``UNKNOWN`` and both
      carry the source of the claim, because an anonymous label is not evidence.

    The run outcome is deliberately **not** a column. ``task_success`` copied onto
    this row would go stale the moment a verifier recorded a later verdict, so the
    outcome is always derived by joining to ``runs`` and ``verifications`` (section
    25).

    What is never stored: the rendered context, the prompt, or any chain of thought.
    Only a fingerprint, a position and a character count (section 19).
    """

    model_config = _FROZEN

    retrieval_session_id: str
    experience_id: str

    id: str = Field(default_factory=new_id)

    rank: int = Field(ge=1)
    """Position in the retrieval result, 1-based, in the order the agent would see
    it (guidance first, then warnings). Recorded as it was, never recomputed."""

    role: UsageRole
    retrieval_score: float
    """The score the ranker produced at the time. Historical evidence: re-scoring
    old rows with today's ranker would invent a past that never happened (section 60)."""

    retrieved_at: AwareDatetime = Field(default_factory=utc_now)

    injected_at: AwareDatetime | None = None
    injection_position: int | None = Field(default=None, ge=1)
    injection_chars: int | None = Field(default=None, ge=0)
    context_fingerprint: str | None = None
    formatter_version: str | None = None

    usage_signal: UsageSignal = UsageSignal.UNKNOWN
    usage_signal_source: UsageSignalSource | None = None
    usage_signal_at: AwareDatetime | None = None

    utility_label: UtilityLabel = UtilityLabel.UNKNOWN
    utility_label_source: UtilitySource | None = None
    utility_label_at: AwareDatetime | None = None

    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keep_signals_and_sources_together(self) -> ExperienceUsage:
        """A claim and its provenance are stored together or not at all.

        ``UNKNOWN`` with a source, or a decided signal with no source, both describe
        a fact table that cannot be audited -- so neither is representable.
        """
        if (self.usage_signal is UsageSignal.UNKNOWN) != (self.usage_signal_source is None):
            raise ValueError(
                "usage_signal_source must be set exactly when usage_signal is not "
                f"UNKNOWN (signal={self.usage_signal.value}, "
                f"source={self.usage_signal_source})"
            )
        if (self.utility_label is UtilityLabel.UNKNOWN) != (self.utility_label_source is None):
            raise ValueError(
                "utility_label_source must be set exactly when utility_label is not "
                f"UNKNOWN (label={self.utility_label.value}, "
                f"source={self.utility_label_source})"
            )
        if self.injected_at is None and (
            self.injection_position is not None
            or self.injection_chars is not None
            or self.context_fingerprint is not None
            or self.formatter_version is not None
        ):
            raise ValueError(
                "injection detail (position, chars, context fingerprint, formatter "
                "version) cannot be recorded without injected_at"
            )
        return self

    # -- derived views -----------------------------------------------------

    @property
    def is_injected(self) -> bool:
        """Whether this experience really entered the agent's context."""
        return self.injected_at is not None

    @property
    def is_adopted(self) -> bool:
        """Whether there is explicit evidence the agent used it."""
        return self.usage_signal is UsageSignal.ADOPTED

    @property
    def is_explicitly_rejected(self) -> bool:
        """Whether a human or agent judged this experience inapplicable."""
        return self.usage_signal is UsageSignal.REJECTED

    # -- update operations -------------------------------------------------

    def with_injection(
        self,
        *,
        injected_at: AwareDatetime,
        context_fingerprint: str | None = None,
        formatter_version: str | None = None,
        injection_position: int | None = None,
        injection_chars: int | None = None,
    ) -> ExperienceUsage:
        """Return a copy marked as injected into the agent's context."""
        return self._replace(
            injected_at=injected_at,
            context_fingerprint=context_fingerprint,
            formatter_version=formatter_version,
            injection_position=injection_position,
            injection_chars=injection_chars,
            updated_at=utc_now(),
        )

    def with_usage_signal(
        self,
        *,
        signal: UsageSignal,
        source: UsageSignalSource | None,
        at: AwareDatetime,
        metadata: JsonObject | None = None,
    ) -> ExperienceUsage:
        """Return a copy carrying ``signal`` asserted by ``source``."""
        return self._replace(
            usage_signal=signal,
            usage_signal_source=source,
            usage_signal_at=at,
            updated_at=at,
            metadata={**self.metadata, **(metadata or {})},
        )

    def with_utility(
        self,
        *,
        label: UtilityLabel,
        source: UtilitySource | None,
        at: AwareDatetime,
        metadata: JsonObject | None = None,
    ) -> ExperienceUsage:
        """Return a copy carrying ``label`` asserted by ``source``."""
        return self._replace(
            utility_label=label,
            utility_label_source=source,
            utility_label_at=at,
            updated_at=at,
            metadata={**self.metadata, **(metadata or {})},
        )

    def _replace(self, **changes: object) -> ExperienceUsage:
        """Build a re-validated copy with ``changes`` applied."""
        data: dict[str, object] = dict(self.model_dump())
        data.update(changes)
        return ExperienceUsage(**data)  # type: ignore[arg-type]


def _with_audit(
    metadata: JsonObject,
    action: str,
    detail: str,
    reason: str | None,
) -> JsonObject:
    """Record an audit entry in a metadata object, keeping earlier ones."""
    history = metadata.get("audit")
    entries: list[JsonValue] = list(history) if isinstance(history, list) else []
    entries.append(
        {
            "action": action,
            "detail": detail,
            "reason": reason,
            "at": utc_now().isoformat(),
        }
    )
    return {**metadata, "audit": entries}


class AdapterSession(BaseModel):
    """The mapping between an external Agent session and one AER run (section 20).

    An external Agent has its own notion of a session -- a Codex conversation, a
    Claude Code window, a Cursor workspace -- and AER has its own notion of a run.
    This row is the correspondence, and it is **persisted** rather than held in
    memory because an Agent process restarts far more often than a task ends
    (section 51): a hook that reconnects after a crash has to find the run it was
    already reporting into.

    ``aer_run_id`` points at the *current* run for this external session. When a
    caller explicitly reopens a session whose run already finished, the previous run
    ids are appended to ``metadata["previous_runs"]`` rather than overwritten: the
    old traces stay reachable, and the fact that one external session produced two
    AER runs is visible instead of implied.

    The external identifiers are recorded and never used as primary keys (section
    17): AER mints its own ids, so a vendor that reuses or reorders identifiers can
    never collide with AER's identity.
    """

    model_config = _FROZEN

    adapter_name: str
    provider: str
    external_session_id: str
    aer_run_id: str

    id: str = Field(default_factory=new_id)

    external_run_id: str | None = None
    protocol_version: str = ""

    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def previous_runs(self) -> tuple[str, ...]:
        """AER runs this external session produced before the current one."""
        recorded = self.metadata.get("previous_runs")
        if not isinstance(recorded, list):
            return ()
        return tuple(str(item) for item in recorded)


class AdapterEventRecord(BaseModel):
    """One external event that has been accepted, and what it became (section 19).

    The idempotency ledger. A vendor hook that retries, a webhook that is delivered
    twice, a session that is replayed -- all of them deliver the same external event
    more than once, and the answer must be "the second delivery is a no-op", not
    "the second delivery is a second event" (section 18).

    The uniqueness key is ``(provider, external_event_id)``, not
    ``(adapter_name, external_event_id)``. An event id belongs to the *vendor's*
    namespace, so two adapters pointed at the same provider must not each be able to
    record the same event; treating the vendor as the namespace makes that
    misconfiguration harmless instead of silently doubling a trace. Which adapter
    accepted it is still recorded, because "which integration produced this?" is a
    question that gets asked later.

    ``external_sequence`` and ``external_timestamp`` are kept verbatim (section 23).
    AER's own ``sequence`` is always arrival order -- rewriting it to match a vendor's
    numbering would mean rewriting history when a late event arrives -- so the
    vendor's ordering is preserved *next to* it rather than instead of it.
    """

    model_config = _FROZEN

    provider: str
    external_event_id: str

    adapter_name: str
    event_type: EventType
    aer_run_id: str

    id: str = Field(default_factory=new_id)

    applied: bool = False
    """Whether the external event has actually been applied to the run.

    A separate flag rather than "``aer_event_id`` is set", because some legitimate
    envelopes produce no single event of their own: a batch may write several, and the
    lifecycle envelopes write none. "Did we finish applying this?" must be answerable
    without inferring it from a nullable id.

    It is set *after* the ledger row is claimed, which is what makes the ledger
    at-most-once: a row that exists but is not applied means an ingest died in the
    middle, and the retry is treated as a duplicate. Losing one event is the trade
    this makes deliberately -- a duplicated event corrupts a statistic silently,
    while a missing one is visible as an unapplied ledger row.
    """

    aer_event_id: int | None = None
    """The AER event this external event produced, when there is exactly one.

    ``None`` for the envelopes that drive the run's lifecycle rather than an event:
    opening a session writes ``TASK_START`` through the normal run start, and a
    closing envelope goes through ``RunContext.finish``. The run id is the link that
    always exists.
    """

    external_sequence: int | None = None
    external_timestamp: AwareDatetime | None = None

    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def is_applied(self) -> bool:
        """Whether this external event has been fully applied to its run."""
        return self.applied
