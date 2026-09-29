"""Central enumerations for the AER runtime.

These are the **only** allowed vocabularies for run status, event type and
verifier type. Business code must never invent string literals for these concepts
(agent.md #10, #11; TASKS.md #4 Task 1.2 / 1.3).

``StrEnum`` is used deliberately:

* members are real ``str`` instances, so they are JSON- and SQLite-friendly;
* ``str(RunStatus.SUCCESS) == "SUCCESS"`` (unlike ``class X(str, Enum)``);
* the value stored in the database is stable and human readable.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Lifecycle status of a single :class:`aer.runtime.models.Run`.

    Four of the five terminal members are **declarations**: somebody -- the agent, or
    the platform it runs on -- said how the work ended. The fifth, ``INCONCLUSIVE``,
    is the absence of a declaration, and it needed its own member because the
    alternative was to spell "nobody said anything" with one of the four (round-8.1.1,
    D-100).

    Overloading ``ABORTED`` for that was the specific mistake: ``ABORTED`` means "the
    agent declared the work abandoned", which drives failure handling everywhere, so an
    integration that closed a run because a *session* ended was manufacturing a
    declaration that nobody made. The distinction is not cosmetic -- it decides whether
    a failed tool call becomes a failure experience.

    * ``RUNNING``          -- not finished.
    * ``SUCCESS``          -- the agent declared the task complete.
    * ``PARTIAL_SUCCESS``  -- the agent declared it partly complete.
    * ``FAILED``           -- the agent declared it failed.
    * ``ABORTED``          -- the agent declared it abandoned.
    * ``INCONCLUSIVE``     -- nobody declared anything; the run is over anyway.

    ``INCONCLUSIVE`` is terminal and, unlike the other four, carries no claim about the
    work at all. It is not a synonym for failure and must never be counted as one. What
    can still settle such a run is *independent evidence*: a required verification that
    passed makes it a verified success (see
    :func:`aer.verification.summary.is_verified_success`), and a required verification
    that failed makes it a proven failure. Without either, its outcome is simply unknown.
    """

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class EventType(StrEnum):
    """Standardised events emitted while an agent executes a task."""

    TASK_START = "TASK_START"

    MODEL_CALL = "MODEL_CALL"
    MODEL_RESULT = "MODEL_RESULT"

    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"

    ERROR = "ERROR"

    RECOVERY_START = "RECOVERY_START"
    RECOVERY_RESULT = "RECOVERY_RESULT"

    VERIFICATION = "VERIFICATION"

    HUMAN_FEEDBACK = "HUMAN_FEEDBACK"

    TASK_END = "TASK_END"


class VerifierType(StrEnum):
    """Verification strategy, **declared in trust order** (highest first).

    The declaration order is the documented trust order of agent.md #18 and of the
    round-4 brief, section 3::

        DETERMINISTIC  >  ENVIRONMENT  >  HUMAN  >  LLM  >  agent self-evaluation

    Iteration therefore yields verifiers from most to least trustworthy, which is
    the order a future ranker or confidence calculation needs. ``LLM`` sits last
    because a model judgement is the weakest *independent* evidence; agent
    self-evaluation is weaker still and is deliberately **not** part of this
    vocabulary -- AER must never let an agent's own claim count as verification
    (TASKS.md #27.7).

    The members were reordered in Milestone 4: the enum previously declared
    ``LLM`` before ``HUMAN`` even though its own docstring claimed to be ordered by
    trustworthiness. Values are unchanged, so stored data is unaffected.
    """

    DETERMINISTIC = "DETERMINISTIC"
    ENVIRONMENT = "ENVIRONMENT"
    HUMAN = "HUMAN"
    LLM = "LLM"


class ExperienceKind(StrEnum):
    """What an Experience is *about*, decided from run facts (round-5 brief, section 27).

    Three kinds, not three models: the lifecycle, storage and repositories are
    identical for all of them, and only the meaning of the fields differs. Three
    parallel classes would have tripled every code path for no benefit.

    * ``SUCCESS``  -- the task was completed and independently confirmed, with no
      repair needed. Low information density, which is why it is only distilled on
      request or when something else made the run interesting.
    * ``RECOVERY`` -- something failed, was repaired, and the task still ended
      confirmed. The most valuable kind AER produces (agent.md #32): it carries the
      failed attempts *and* the fix.
    * ``FAILURE``  -- the task did not satisfy its goal, either because the run
      failed or because an independent verifier disagreed with the agent. Still
      worth keeping: it is the only evidence AER has about what does **not** work.
    """

    SUCCESS = "SUCCESS"
    RECOVERY = "RECOVERY"
    FAILURE = "FAILURE"


class ExperienceStatus(StrEnum):
    """Lifecycle status of an :class:`~aer.runtime.models.Experience`.

    Declared in lifecycle order. Only ``RAW``, ``DISTILLED`` and ``VERIFIED`` are
    reachable in this milestone; the rest are part of the agreed vocabulary but have
    no producer yet (see :mod:`aer.runtime.lifecycle` and ``docs/DECISIONS.md``
    D-029).
    """

    RAW = "RAW"
    DISTILLED = "DISTILLED"
    VERIFIED = "VERIFIED"
    REUSED = "REUSED"
    PROVEN = "PROVEN"
    TRAINING_CANDIDATE = "TRAINING_CANDIDATE"
    TRAINING_DATA = "TRAINING_DATA"
    DEPRECATED = "DEPRECATED"


class DistillationTrigger(StrEnum):
    """Why a run was considered worth distilling.

    Recorded on every distillation so the *reason* survives in the data and a
    future cost model can check whether the policy is worth its tokens.
    """

    ERROR = "ERROR"
    """The run recorded at least one error."""

    RECOVERY = "RECOVERY"
    """The run recorded a recovery attempt (successful or not)."""

    HUMAN_FEEDBACK = "HUMAN_FEEDBACK"
    """A human commented on the run."""

    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    """An independent verifier judged the outcome unsatisfied."""

    FAILED_RUN = "FAILED_RUN"
    """The run ended in FAILED, ABORTED or PARTIAL_SUCCESS.

    Needed on its own because a run can end without recording any error at all --
    the agent simply gives up -- and that trajectory is exactly what a failure
    experience is for.

    ``INCONCLUSIVE`` is deliberately **not** in that list: it means nobody declared an
    outcome, which is not the agent saying "this did not work". A missing declaration
    is not a failure declaration, and reading it as one would put a claim in the store
    that no participant ever made (round-8.1.1, D-100).
    """

    UNVERIFIED_SUCCESS = "UNVERIFIED_SUCCESS"
    """The agent declared success, but no independent verification confirms it."""

    EXPLICIT_HIGH_VALUE = "EXPLICIT_HIGH_VALUE"
    """The caller asked for this run to be kept regardless of the signals."""


class RetrievalMode(StrEnum):
    """How much of the experience store a retrieval query is allowed to see.

    Declared in **increasing permissiveness**, which is also the trust order:
    a caller asking for guidance wants an answer it can act on, and widening the
    mode trades that guarantee for coverage. Keeping them in one vocabulary (and
    in this module, per the project-wide "enums live here" rule) means the policy
    and the formatter cannot drift apart on what a mode means.

    * ``GUIDANCE``   -- only independently confirmed outcomes (``SUCCESS`` or
      ``RECOVERY`` with ``status >= VERIFIED`` and ``outcome_verified``), plus
      ``FAILURE`` records demoted to warnings. This is the default because it is
      the only mode whose output may be presented as something that *works*.
    * ``DIAGNOSTIC`` -- everything ``GUIDANCE`` returns, plus unverified
      observations, which are labelled as such and never called a solution.
    * ``ALL``        -- ``DIAGNOSTIC`` plus deprecated experiences, and only when
      the caller asks for them explicitly. Deprecated knowledge is understood to
      be wrong, so it never leaks in by default.
    """

    GUIDANCE = "GUIDANCE"
    DIAGNOSTIC = "DIAGNOSTIC"
    ALL = "ALL"


class ProjectionAction(StrEnum):
    """What a projection pass did to one experience.

    Recorded instead of a boolean because "wrote it" and "removed it because the
    store no longer has it" are opposite outcomes that both mean success, and a
    caller that logs ``projected=True`` for a deletion has lost the plot.
    """

    PROJECTED = "PROJECTED"
    """The index now carries this experience's current content."""

    REMOVED = "REMOVED"
    """The store no longer has it, so its projection was deleted."""


class UsageRole(StrEnum):
    """The slot one experience occupied in a retrieval result (Milestone 7).

    Recorded at retrieval time rather than recomputed later from
    :class:`~aer.runtime.enums.ExperienceKind`, because the retrieval policy is
    allowed to change: which kind lands where is a *policy* decision, and a report
    that reclassified history under today's policy would silently rewrite the past
    (round-7 brief, section 9).

    The three members mirror the three things a result can say. ``GUIDANCE`` is the
    ``guidance`` list -- the part a caller may act on. ``WARNING`` is a confirmed
    failure: something that was tried and did **not** work. ``OBSERVATION`` is an
    unverified record, which is useful precisely because it is labelled as not
    confirmed. Warnings and observations share one list in
    :class:`~aer.knowledge.models.RetrievalResult`, but conflating them here would
    lose the difference between "we know this fails" and "this once happened".
    """

    GUIDANCE = "GUIDANCE"
    WARNING = "WARNING"
    OBSERVATION = "OBSERVATION"


class UsageSignal(StrEnum):
    """Whether an agent actually used a retrieved experience (section 10).

    The whole point of the milestone is that this is **unknown by default**. A
    retrieval result is a list of candidates; only an explicit signal from someone
    who saw the agent decide may move a row off ``UNKNOWN``.

    * ``UNKNOWN``   -- no reliable evidence about what the agent did with it. Not a
      failure: it is the honest state of almost every row.
    * ``ADOPTED``   -- there is explicit evidence the agent used the experience.
    * ``IGNORED``   -- the experience demonstrably entered the context and was not
      used.
    * ``REJECTED``  -- a human or an agent judged it inapplicable to this task.

    Text similarity and similar tool calls are explicitly **not** signals
    (section 22): inferring adoption from behaviour is a later, confidence-carrying
    feature, not something to smuggle into a fact table.
    """

    UNKNOWN = "UNKNOWN"
    ADOPTED = "ADOPTED"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"


class UsageSignalSource(StrEnum):
    """Who asserted a :class:`UsageSignal` (section 11).

    Kept separate from the signal because the same claim means different things
    depending on where it came from, and a later stage has to be able to prefer a
    human's judgement over an adapter's guess. A signal never travels without its
    source.
    """

    AGENT = "AGENT"
    HUMAN = "HUMAN"
    ADAPTER = "ADAPTER"
    EVALUATOR = "EVALUATOR"
    SYSTEM = "SYSTEM"


class UtilityLabel(StrEnum):
    """Whether an experience helped, once the outcome is known (section 12).

    Deliberately **not** an alias of :class:`UsageSignal`: an agent can adopt an
    experience that is wrong, so ``ADOPTED`` does not imply ``HELPFUL``. This is the
    judgement that needs a task outcome and, usually, a human or an evaluator.
    """

    UNKNOWN = "UNKNOWN"
    HELPFUL = "HELPFUL"
    NEUTRAL = "NEUTRAL"
    HARMFUL = "HARMFUL"


class UtilitySource(StrEnum):
    """Who asserted a :class:`UtilityLabel`.

    ``SYSTEM`` is in the vocabulary but must never be assigned merely because a run
    ended in ``SUCCESS`` (section 13): the experience may have been ignored while the
    agent solved the task by itself.
    """

    HUMAN = "HUMAN"
    EVALUATOR = "EVALUATOR"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class SessionAssignment(StrEnum):
    """Which arm of a future experiment a retrieval session belongs to (section 31).

    Reserved now, unused now. It exists so that the holdout design of section 32 does
    not require a schema change when it arrives: a session already says whether it
    was allowed to inject or was held back.
    """

    NONE = "NONE"
    """No experiment was running. The value on every session today."""

    TREATMENT = "TREATMENT"
    """The retrieved experiences could be injected."""

    HOLDOUT = "HOLDOUT"
    """The retrieved experiences were deliberately withheld, to measure the
    counterfactual."""


class RunOutcome(StrEnum):
    """The outcome class of a run, as effectiveness statistics need it (section 26).

    Five classes rather than a boolean, because "the task failed" and "nobody ever
    checked" are different facts and only one of them belongs in a denominator.
    ``UNVERIFIED`` is explicitly **not** a failure (section 26): counting an
    unobserved run as a negative outcome would make every experience look worse the
    less it was measured.

    Precedence when several apply is decided in
    :func:`aer.usage.effectiveness.classify_outcome`, in one place, with the ordering
    written down.
    """

    RUNNING = "RUNNING"
    """The run has not finished. No outcome exists yet."""

    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    """The agent declared success and the required verifications passed."""

    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    """An independent verification confirmed the goal was not met."""

    RUN_FAILED = "RUN_FAILED"
    """The agent declared the run failed, aborted or partly succeeded, and no
    verification confirmed the failure."""

    UNVERIFIED = "UNVERIFIED"
    """The run finished without any verification result deciding its outcome."""


class AdapterSessionOutcome(StrEnum):
    """What opening an external Agent session actually did (Milestone 8).

    Three outcomes rather than a boolean, because "I resumed the run you were already
    in" and "I started a second run for the same external session" are different
    facts about the relationship between an external Agent and AER's traces. A caller
    that reconnected and got ``REOPENED`` when it expected ``RESUMED`` has just
    discovered that its previous episode was already closed -- which is exactly the
    kind of thing that must not be inferred (round-8 brief, sections 21 and 43).
    """

    STARTED = "STARTED"
    """No mapping existed; a new AER run was created for this external session."""

    RESUMED = "RESUMED"
    """The mapping existed and its run was still ``RUNNING``; that run was reused."""

    REOPENED = "REOPENED"
    """The mapping existed and its run was already terminal, and the caller explicitly
    asked for a new episode (``on_terminal="reopen"``). The previous run is kept and
    recorded, never rewritten."""


class AdapterIngestOutcome(StrEnum):
    """What delivering one external event to AER did (Milestone 8).

    Three outcomes, because "nothing happened" has two very different causes and an
    integration that cannot tell them apart will mis-diagnose itself: a duplicate
    delivery means the platform is retrying (working as designed), while a dropped
    event means the adapter decided this vendor event carries no evidence (also
    working as designed). Neither is a failure, and neither should look like a write.
    """

    APPLIED = "APPLIED"
    """Translated, deduplicated and written to the run."""

    DUPLICATE = "DUPLICATE"
    """This external event id was already accepted; nothing was written."""

    IGNORED = "IGNORED"
    """The adapter translated the vendor event into no protocol envelopes."""
