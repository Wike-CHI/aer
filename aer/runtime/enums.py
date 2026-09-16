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
    """Lifecycle status of a single :class:`aer.runtime.models.Run`."""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


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
    """

    UNVERIFIED_SUCCESS = "UNVERIFIED_SUCCESS"
    """The agent declared success, but no independent verification confirms it."""

    EXPLICIT_HIGH_VALUE = "EXPLICIT_HIGH_VALUE"
    """The caller asked for this run to be kept regardless of the signals."""
