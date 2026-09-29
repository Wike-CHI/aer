"""What the usage evidence adds up to -- and what it does not claim.

The report this module produces answers the questions the milestone was written for
(round-7 brief, final acceptance)::

    how often was this retrieved?
    how often did it really enter an agent's context?
    how often was it adopted, ignored, explicitly rejected?
    in how many real tasks did that happen, and how did those tasks end?
    did anyone report it as harmful?

Four boundaries are built into the code rather than left to the reader.

**Retrieved is not injected.** ``retrieval_count`` and ``injection_count`` are
separate columns and the outcome metrics are computed over injected rows only. A
result the agent never saw is not exposure (section 2).

**Adopted is not helpful.** ``explicit_adoption_count`` and ``helpful_count`` come
from two different signals with two different sources, and neither is derived from
the other (section 12).

**Task success is not effect.** ``observed_success_rate`` is named for what it is: a
rate observed among runs where the experience happened to be present. It is
``P(success | injected)``, never ``P(success | do(injected))`` -- the two are equal
only under a randomised experiment, which this milestone deliberately does not run
(sections 30 and 32).

**Unverified is not failure.** A run nobody checked lands in ``unverified_runs`` and
is excluded from every rate. Counting it as a negative outcome would make an
experience look worse the less it was measured (section 26).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aer.exceptions import RecordNotFoundError
from aer.runtime.enums import RunOutcome, RunStatus, UsageSignal, UtilityLabel
from aer.runtime.models import ExperienceUsage, RetrievalSession, Run
from aer.storage.repositories import (
    ExperienceRepository,
    ExperienceUsageRepository,
    RetrievalSessionRepository,
    RunRepository,
    VerificationRepository,
)
from aer.verification.summary import VerificationSummary, is_verified_success

__all__ = [
    "ExperienceEffectivenessReport",
    "ExperienceEffectivenessService",
    "classify_outcome",
]

#: Run statuses that mean "the agent did not declare success".
#:
#: ``INCONCLUSIVE`` is deliberately absent. It is the one terminal status that declares
#: nothing, so counting it here would record "nobody said how this went" as "the agent
#: said it failed" -- and would make every unmeasured integration look worse the less it
#: was measured, which is the mistake ``RunOutcome.UNVERIFIED`` exists to avoid
#: (round-8.1.1, D-100). A verified ``INCONCLUSIVE`` run is caught earlier by
#: :func:`~aer.verification.summary.is_verified_success` and counted as a success.
_UNSUCCESSFUL_STATUSES = frozenset({RunStatus.FAILED, RunStatus.ABORTED, RunStatus.PARTIAL_SUCCESS})

#: Outcomes that rest on a recorded verdict, and so may appear in a rate.
_VERDICT_OUTCOMES = frozenset({RunOutcome.VERIFIED_SUCCESS, RunOutcome.VERIFIED_FAILURE})


def classify_outcome(run: Run, summary: VerificationSummary | None) -> RunOutcome:
    """Classify one run's outcome, with the precedence written down here and only here.

    The order is deliberate:

    1. a ``RUNNING`` run has no outcome yet, whatever verdicts exist for it;
    2. ``VERIFIED_SUCCESS`` -- the one combination of agent declaration and
       independent confirmation that earns it (see
       :func:`~aer.verification.summary.is_verified_success`);
    3. ``VERIFIED_FAILURE`` -- an independent required check confirmed the goal was
       not met. Checked *before* the agent's own status because evidence outranks a
       declaration: a run that ended ``FAILED`` and was confirmed unmet is better
       described as verified-failure than as merely failed;
    4. ``RUN_FAILED`` -- the agent declared the run failed, aborted or partly
       succeeded, and nothing confirmed the failure. Passing checks do not make this
       a success: they show the environment is fine, not that the agent finished;
    5. ``UNVERIFIED`` -- everything else, including the very common case of an agent
       declaring success with nobody checking. Not a failure (section 26). This is also
       where an ``INCONCLUSIVE`` run lands when nothing was verified: "nobody said how
       this ended and nobody checked" is unknown, not failed, and it can still be
       promoted to ``VERIFIED_SUCCESS`` the moment a required check passes.

    ``summary`` is ``None`` when the run has no verdicts at all, which is different
    from a summary of zeroes and is handled by the same branches.
    """
    if run.status is RunStatus.RUNNING:
        return RunOutcome.RUNNING
    if summary is not None and is_verified_success(run.status, summary):
        return RunOutcome.VERIFIED_SUCCESS
    if summary is not None and summary.required_failed > 0:
        return RunOutcome.VERIFIED_FAILURE
    if run.status in _UNSUCCESSFUL_STATUSES:
        return RunOutcome.RUN_FAILED
    return RunOutcome.UNVERIFIED


@dataclass(frozen=True, slots=True)
class ExperienceEffectivenessReport:
    """The observed usage evidence for one experience.

    Every count is a count of *rows or runs*, never a score: nothing here is
    smoothed, weighted or inferred, because the whole value of this table is that a
    later reader can re-derive any number in it from the stored facts.

    Two properties of the counts are worth stating because they are easy to misread:

    * ``retrieval_count`` counts retrieval **sessions**, so the same experience found
      by two different agents on two different days counts twice -- as it should;
    * the outcome counts are over **distinct runs where the experience was injected**,
      not over usage rows. Ten retrievals inside one run are one run's worth of
      evidence, and counting them ten times is how an experience comes to look proven
      on the strength of a retry loop.
    """

    experience_id: str

    retrieval_count: int
    """Number of retrieval sessions that returned this experience."""

    injection_count: int
    """Number of those sessions in which it actually entered the agent's context."""

    explicit_adoption_count: int
    explicit_ignore_count: int
    explicit_rejection_count: int

    helpful_count: int
    neutral_count: int
    harmful_count: int

    distinct_target_runs: int
    """Distinct runs that had this experience injected. The exposure population."""

    verified_success_runs: int
    verified_failure_runs: int
    run_failed_runs: int
    unverified_runs: int
    running_runs: int

    unattributed_usage_count: int
    """Usage rows whose retrieval was never linked to a run (section 24).

    Reported rather than dropped: a large number here means sessions are being
    recorded without a run, and the rates below are then describing less of the data
    than a reader would assume.
    """

    adopted_runs: int
    """Distinct runs in which the experience was explicitly adopted."""

    adopted_verified_success_runs: int
    adopted_verified_failure_runs: int

    injected_verified_total: int
    """Distinct injected runs with a verdict; the denominator of the headline rate."""

    adopted_verified_total: int

    observed_success_rate: float | None
    """``verified_success_runs / injected_verified_total``, or ``None`` when the
    denominator is zero.

    The headline number. It is the *injected and independently verified* rate, which
    is the strictest population available without an experiment -- and it is still a
    correlation, not an effect.
    """

    injected_verified_success_rate: float | None
    """The same value as :attr:`observed_success_rate`, under an explicit name.

    Both exist because they answer slightly different questions: the headline name
    may later widen its population, while this one pins the denominator so a change
    to the headline cannot silently change the number a comparison was made against.
    """

    adopted_verified_success_rate: float | None
    """The adoption subset, reported **separately** and never merged with the above
    (section 29): runs where an explicit signal says the agent used the experience.
    """

    @property
    def verified_total(self) -> int:
        """Distinct injected runs whose outcome was decided by verification."""
        return self.verified_success_runs + self.verified_failure_runs

    @property
    def outcome_total(self) -> int:
        """Distinct injected runs, whatever their outcome class."""
        return (
            self.verified_success_runs
            + self.verified_failure_runs
            + self.run_failed_runs
            + self.unverified_runs
            + self.running_runs
        )

    @property
    def has_evidence(self) -> bool:
        """Whether this experience was ever retrieved at all."""
        return self.retrieval_count > 0

    @property
    def never_injected(self) -> bool:
        """Whether it was retrieved but never reached an agent."""
        return self.retrieval_count > 0 and self.injection_count == 0

    def describe(self) -> str:
        """A one-block summary, for a log line or a status command."""
        rate = "n/a" if self.observed_success_rate is None else f"{self.observed_success_rate:.0%}"
        return (
            f"{self.experience_id}: retrieved {self.retrieval_count}x, "
            f"injected {self.injection_count}x, "
            f"adopted {self.explicit_adoption_count}x, "
            f"ignored {self.explicit_ignore_count}x, "
            f"rejected {self.explicit_rejection_count}x, "
            f"runs {self.distinct_target_runs} "
            f"(verified success {self.verified_success_runs}, "
            f"verified failure {self.verified_failure_runs}, "
            f"unverified {self.unverified_runs}), "
            f"observed success rate {rate}, harmful {self.harmful_count}"
        )


class ExperienceEffectivenessService:
    """Joins usage to runs and verdicts, in a fixed number of queries.

    The service exists as its own class rather than as methods on a repository for
    the reason section 55 gives: a repository stores one table, and this answers a
    question that spans three of them. Putting the join in a repository would make
    that repository the only one that knows about the other two.
    """

    def __init__(
        self,
        *,
        experiences: ExperienceRepository,
        usage: ExperienceUsageRepository,
        sessions: RetrievalSessionRepository,
        runs: RunRepository,
        verifications: VerificationRepository,
    ) -> None:
        self._experiences = experiences
        self._usage = usage
        self._sessions = sessions
        self._runs = runs
        self._verifications = verifications

    def report(self, experience_id: str) -> ExperienceEffectivenessReport:
        """The report for one experience.

        Raises:
            RecordNotFoundError: no such experience exists. A missing experience is
                reported as missing rather than as a report full of zeroes, because
                zeroes are a real and common state (an experience nobody has
                retrieved yet) and the two must not look alike.
        """
        return self.reports([experience_id])[0]

    def reports(self, experience_ids: Sequence[str]) -> list[ExperienceEffectivenessReport]:
        """Reports for several experiences, in four queries regardless of how many.

        One query each for the usage rows, the sessions they belong to, the target
        runs, and the verdict aggregates. The per-experience alternative --
        re-reading runs and verdicts for each id -- is the N+1 that section 56 names.
        """
        unique = list(dict.fromkeys(experience_ids))
        if not unique:
            return []

        known = self._experiences.get_many(unique)
        missing = [experience_id for experience_id in unique if experience_id not in known]
        if missing:
            raise RecordNotFoundError(f"Experience(s) not found: {missing}")

        usages = self._usage.list_for_experiences(unique)
        sessions = self._sessions.get_many(sorted({usage.retrieval_session_id for usage in usages}))
        run_ids = sorted(
            {session.run_id for session in sessions.values() if session.run_id is not None}
        )
        runs = self._runs.get_many(run_ids)
        summaries = self._verifications.summaries_by_run(run_ids)

        grouped: dict[str, list[ExperienceUsage]] = {}
        for usage in usages:
            grouped.setdefault(usage.experience_id, []).append(usage)

        return [
            _build_report(experience_id, grouped.get(experience_id, []), sessions, runs, summaries)
            for experience_id in unique
        ]

    def report_all(self, *, limit: int = 1000) -> list[ExperienceEffectivenessReport]:
        """Reports for every experience that has any usage, in ``limit`` batches.

        Iterates the usage table rather than the experience store: a report about
        behaviour that does not exist is not worth computing, and a store with ten
        thousand undistilled-to-usage experiences would otherwise make this
        unusably slow.
        """
        return self.reports(self._usage.distinct_experience_ids()[:limit])


def _build_report(
    experience_id: str,
    usages: Sequence[ExperienceUsage],
    sessions: dict[str, RetrievalSession],
    runs: dict[str, Run],
    summaries: dict[str, VerificationSummary],
) -> ExperienceEffectivenessReport:
    """Fold one experience's usage rows into a report.

    Kept as a module function rather than a method so the whole computation is
    visible at once: every number in the report is derived from the five loops below
    and nothing else reads the rows.
    """
    injection_count = 0
    adoptions = ignores = rejections = 0
    helpful = neutral = harmful = 0
    unattributed = 0

    injected_outcomes: dict[str, RunOutcome] = {}
    adopted_outcomes: dict[str, RunOutcome] = {}

    for usage in usages:
        if usage.is_injected:
            injection_count += 1
        if usage.usage_signal is UsageSignal.ADOPTED:
            adoptions += 1
        elif usage.usage_signal is UsageSignal.IGNORED:
            ignores += 1
        elif usage.usage_signal is UsageSignal.REJECTED:
            rejections += 1

        if usage.utility_label is UtilityLabel.HELPFUL:
            helpful += 1
        elif usage.utility_label is UtilityLabel.NEUTRAL:
            neutral += 1
        elif usage.utility_label is UtilityLabel.HARMFUL:
            harmful += 1

        session = sessions.get(usage.retrieval_session_id)
        run_id = None if session is None else session.run_id
        run = None if run_id is None else runs.get(run_id)
        if run is None or run_id is None:
            unattributed += 1
            continue

        outcome = classify_outcome(run, summaries.get(run_id))
        if usage.is_injected:
            injected_outcomes.setdefault(run_id, outcome)
        if usage.usage_signal is UsageSignal.ADOPTED:
            adopted_outcomes.setdefault(run_id, outcome)

    verified_success_runs = _count(injected_outcomes, RunOutcome.VERIFIED_SUCCESS)
    verified_failure_runs = _count(injected_outcomes, RunOutcome.VERIFIED_FAILURE)
    injected_verified_total = verified_success_runs + verified_failure_runs
    adopted_verified_success_runs = _count(adopted_outcomes, RunOutcome.VERIFIED_SUCCESS)
    adopted_verified_failure_runs = _count(adopted_outcomes, RunOutcome.VERIFIED_FAILURE)
    adopted_verified_total = adopted_verified_success_runs + adopted_verified_failure_runs

    injected_rate = _rate(verified_success_runs, injected_verified_total)
    adopted_rate = _rate(adopted_verified_success_runs, adopted_verified_total)

    return ExperienceEffectivenessReport(
        experience_id=experience_id,
        retrieval_count=len(usages),
        injection_count=injection_count,
        explicit_adoption_count=adoptions,
        explicit_ignore_count=ignores,
        explicit_rejection_count=rejections,
        helpful_count=helpful,
        neutral_count=neutral,
        harmful_count=harmful,
        distinct_target_runs=len(injected_outcomes),
        verified_success_runs=verified_success_runs,
        verified_failure_runs=verified_failure_runs,
        run_failed_runs=_count(injected_outcomes, RunOutcome.RUN_FAILED),
        unverified_runs=_count(injected_outcomes, RunOutcome.UNVERIFIED),
        running_runs=_count(injected_outcomes, RunOutcome.RUNNING),
        unattributed_usage_count=unattributed,
        adopted_runs=len(adopted_outcomes),
        adopted_verified_success_runs=adopted_verified_success_runs,
        adopted_verified_failure_runs=adopted_verified_failure_runs,
        injected_verified_total=injected_verified_total,
        adopted_verified_total=adopted_verified_total,
        observed_success_rate=injected_rate,
        injected_verified_success_rate=injected_rate,
        adopted_verified_success_rate=adopted_rate,
    )


def _count(outcomes: dict[str, RunOutcome], wanted: RunOutcome) -> int:
    """How many runs in ``outcomes`` landed in ``wanted``."""
    return sum(1 for outcome in outcomes.values() if outcome is wanted)


def _rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``, or ``None`` when nothing can be divided.

    ``None`` rather than ``0.0``: "no run was ever verified" and "no verified run
    succeeded" are different answers, and a zero would let a caller that only reads
    the number report the first as the second.
    """
    if denominator == 0:
        return None
    return numerator / denominator
