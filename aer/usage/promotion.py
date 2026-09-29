"""When a verified claim has earned the next lifecycle status -- and when it has not.

Two promotions, two very different bars (round-7 brief, sections 33-39)::

    VERIFIED  ->  REUSED   "it was really used, elsewhere"
    REUSED    ->  PROVEN   "it was explicitly adopted, repeatedly, and it worked"

The distance between them is the point. ``REUSED`` is cheap and deliberately so: an
experience injected into a run other than the one it came from has demonstrably
survived contact with a second task. Nothing about that requires the second task to
have succeeded, because failing to help is not the same as failing to be used
(section 35). ``PROVEN`` is expensive: it needs adoption **signals**, not just
exposure, from several distinct runs, most of which must have ended in a verified
success, with no harmful feedback anywhere.

Three rules carry most of the weight:

**The source run is not a reuse.** An experience distilled from run A and injected
back into run A proves nothing about whether it generalises -- the run that produced
it is the one case where it is guaranteed to be relevant (section 34).

**Retrieval alone is not reuse.** Only an *injection* counts, because only an
injection means an agent actually received it (section 33).

**No adoption evidence means no ``PROVEN``.** A hundred injections with every signal
``UNKNOWN`` are allowed to reach ``REUSED`` and are never allowed past it (section
38): the system does not know whether anyone even read the experience, and promoting
on that basis would be manufacturing evidence out of exposure.

Nothing here is causal. ``PROVEN`` means "adopted in several distinct runs that
succeeded, with a verification behind each success" -- co-occurrence, not proof of
mechanism (section 39). Establishing the difference needs a randomised holdout, which
section 32 reserves and this milestone does not build.
"""

from __future__ import annotations

from dataclasses import dataclass

from aer.exceptions import RecordNotFoundError
from aer.runtime.enums import ExperienceStatus
from aer.runtime.lifecycle import is_at_least
from aer.runtime.models import Experience
from aer.storage.repositories import (
    ExperienceRepository,
    ExperienceSourceRepository,
    ExperienceUsageRepository,
)
from aer.usage.effectiveness import (
    ExperienceEffectivenessReport,
    ExperienceEffectivenessService,
)

__all__ = [
    "DEFAULT_PROMOTION_POLICY",
    "ExperiencePromotionService",
    "PromotionDecision",
    "PromotionPolicy",
]


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """The thresholds an experience must clear to move up a status.

    Every number is configuration, as section 37 requires, and every one of them is
    about *adopted* runs. Exposure-based thresholds were rejected: they would let an
    experience become ``PROVEN`` on the strength of being injected a lot, which is a
    statement about the retriever, not about the experience.
    """

    minimum_adopted_runs: int = 5
    """Distinct runs in which the experience was explicitly adopted."""

    minimum_adopted_verified_successes: int = 4
    """Distinct adopted runs whose outcome verification confirmed as success."""

    minimum_adopted_success_rate: float = 0.80
    """Lower bound on the adopted verified success rate."""

    def __post_init__(self) -> None:
        if self.minimum_adopted_runs < 1:
            raise ValueError("minimum_adopted_runs must be at least 1")
        if self.minimum_adopted_verified_successes < 0:
            raise ValueError("minimum_adopted_verified_successes must not be negative")
        if self.minimum_adopted_verified_successes > self.minimum_adopted_runs:
            raise ValueError(
                "minimum_adopted_verified_successes cannot exceed minimum_adopted_runs: "
                "a successful run is a subset of the adopted runs"
            )
        if not 0.0 <= self.minimum_adopted_success_rate <= 1.0:
            raise ValueError("minimum_adopted_success_rate must be within [0, 1]")

    def evaluate(
        self,
        *,
        experience_id: str,
        status: ExperienceStatus,
        report: ExperienceEffectivenessReport,
        injected_run_ids: frozenset[str],
        source_run_ids: frozenset[str],
    ) -> PromotionDecision:
        """Decide the next status for one experience, with reasons either way.

        Args:
            experience_id: For the returned decision.
            status: The experience's current status.
            report: Its effectiveness report.
            injected_run_ids: Distinct runs the experience was injected into.
            source_run_ids: The runs that produced it.

        Returns:
            A decision whose ``reasons`` explain it in both directions -- a refusal is
            as much an answer as a promotion, and an operator needs to know which
            threshold was missed.
        """
        if status is ExperienceStatus.DEPRECATED:
            return PromotionDecision(
                experience_id=experience_id,
                current_status=status,
                eligible=False,
                suggested_status=None,
                reasons=(
                    "the experience has been withdrawn; withdrawn knowledge is never promoted",
                ),
            )
        if is_at_least(status, ExperienceStatus.PROVEN):
            return PromotionDecision(
                experience_id=experience_id,
                current_status=status,
                eligible=False,
                suggested_status=None,
                reasons=(f"already at {status.value}; promotion is a one-way ladder",),
            )
        if is_at_least(status, ExperienceStatus.REUSED):
            return self._evaluate_proven(experience_id, status, report)

        if not is_at_least(status, ExperienceStatus.VERIFIED):
            return PromotionDecision(
                experience_id=experience_id,
                current_status=status,
                eligible=False,
                suggested_status=None,
                reasons=(
                    f"{status.value} is below VERIFIED: the claim itself has not been "
                    "confirmed yet, so there is nothing reliable to reuse",
                ),
            )
        return self._evaluate_reused(experience_id, status, injected_run_ids, source_run_ids)

    def _evaluate_reused(
        self,
        experience_id: str,
        status: ExperienceStatus,
        injected_run_ids: frozenset[str],
        source_run_ids: frozenset[str],
    ) -> PromotionDecision:
        """``VERIFIED -> REUSED``: has it been injected anywhere it did not come from?"""
        if not injected_run_ids:
            reason = (
                "it has never been injected into a run; being retrieved is not reuse (section 33)"
            )
        else:
            elsewhere = injected_run_ids - source_run_ids
            if not elsewhere:
                reason = (
                    f"it has only been injected into its own source run(s) "
                    f"{sorted(source_run_ids)}; the run that produced an experience "
                    "cannot count as proof that it generalises (section 34)"
                )
            else:
                return PromotionDecision(
                    experience_id=experience_id,
                    current_status=status,
                    eligible=True,
                    suggested_status=ExperienceStatus.REUSED,
                    reasons=(
                        f"injected into {len(elsewhere)} run(s) other than its source "
                        f"run(s): {sorted(elsewhere)}",
                    ),
                )
        return PromotionDecision(
            experience_id=experience_id,
            current_status=status,
            eligible=False,
            suggested_status=None,
            reasons=(reason,),
        )

    def _evaluate_proven(
        self,
        experience_id: str,
        status: ExperienceStatus,
        report: ExperienceEffectivenessReport,
    ) -> PromotionDecision:
        """``REUSED -> PROVEN``: adopted, several times, and confirmed to work."""
        shortfalls: list[str] = []

        if report.adopted_runs < self.minimum_adopted_runs:
            detail = (
                f"adopted in {report.adopted_runs} distinct run(s), needs "
                f"{self.minimum_adopted_runs}"
            )
            if report.explicit_adoption_count == 0:
                detail += (
                    "; no adoption signal has ever been recorded, and exposure alone "
                    "is not evidence that anyone used it (section 38)"
                )
            shortfalls.append(detail)

        if report.adopted_verified_success_runs < self.minimum_adopted_verified_successes:
            shortfalls.append(
                f"adopted in {report.adopted_verified_success_runs} verified-successful "
                f"run(s), needs {self.minimum_adopted_verified_successes}"
            )

        rate = report.adopted_verified_success_rate
        if rate is None:
            shortfalls.append(
                "no adopted run has a verification verdict, so no success rate can be observed"
            )
        elif rate < self.minimum_adopted_success_rate:
            shortfalls.append(
                f"adopted success rate is {rate:.0%}, needs {self.minimum_adopted_success_rate:.0%}"
            )

        if report.harmful_count > 0:
            shortfalls.append(
                f"{report.harmful_count} harmful utility report(s) stand against it; "
                "a high success rate does not cancel a confirmed harm (section 73)"
            )

        if shortfalls:
            return PromotionDecision(
                experience_id=experience_id,
                current_status=status,
                eligible=False,
                suggested_status=None,
                reasons=tuple(shortfalls),
            )

        return PromotionDecision(
            experience_id=experience_id,
            current_status=status,
            eligible=True,
            suggested_status=ExperienceStatus.PROVEN,
            reasons=(
                f"adopted in {report.adopted_runs} distinct run(s), "
                f"{report.adopted_verified_success_runs} of them verified successes "
                f"({rate:.0%} adopted success rate), with no harmful feedback",
            ),
        )


#: The default policy: the thresholds section 37 names as an example.
DEFAULT_PROMOTION_POLICY = PromotionPolicy()


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """What the policy decided, and why.

    ``reasons`` is populated in both directions on purpose. A refusal that says
    nothing is indistinguishable from a bug, and the operator question -- "why is
    this not proven yet?" -- is the one this table exists to answer.
    """

    experience_id: str
    current_status: ExperienceStatus
    eligible: bool
    suggested_status: ExperienceStatus | None
    reasons: tuple[str, ...]

    @property
    def is_noop(self) -> bool:
        """Whether there was nothing to do (already at or past the top)."""
        return not self.eligible and self.suggested_status is None

    def describe(self) -> str:
        """A one-line explanation."""
        if self.eligible and self.suggested_status is not None:
            head = f"{self.current_status.value} -> {self.suggested_status.value}"
        else:
            head = f"{self.current_status.value} (no promotion)"
        return f"{self.experience_id}: {head} -- {'; '.join(self.reasons)}"


class ExperiencePromotionService:
    """Evaluates and applies promotion for one experience at a time.

    Promotion is an explicit operation, never a side effect of retrieval or of a
    signal write: a status that changed itself whenever a counter moved would make
    the lifecycle impossible to audit, and the thresholds are meant to be read before
    they are applied.
    """

    def __init__(
        self,
        *,
        experiences: ExperienceRepository,
        sources: ExperienceSourceRepository,
        usage: ExperienceUsageRepository,
        effectiveness: ExperienceEffectivenessService,
        policy: PromotionPolicy | None = None,
    ) -> None:
        self._experiences = experiences
        self._sources = sources
        self._usage = usage
        self._effectiveness = effectiveness
        self._policy = policy or DEFAULT_PROMOTION_POLICY

    @property
    def policy(self) -> PromotionPolicy:
        """The thresholds in force for this service."""
        return self._policy

    def evaluate(
        self,
        experience_id: str,
        *,
        policy: PromotionPolicy | None = None,
    ) -> PromotionDecision:
        """Decide the next status without changing anything.

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        experience = self._experiences.get(experience_id)
        if experience is None:
            raise RecordNotFoundError(f"Experience not found: {experience_id}")
        report = self._effectiveness.report(experience_id)
        return (policy or self._policy).evaluate(
            experience_id=experience_id,
            status=experience.status,
            report=report,
            injected_run_ids=frozenset(self._usage.injected_run_ids(experience_id)),
            source_run_ids=frozenset(self._sources.get_runs(experience_id)),
        )

    def promote(
        self,
        experience_id: str,
        *,
        policy: PromotionPolicy | None = None,
        reason: str | None = None,
    ) -> Experience | None:
        """Apply the promotion if the policy allows it.

        Returns:
            The updated experience, or ``None`` when nothing was eligible. ``None``
            is the ordinary answer for most experiences, not an error.

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        decision = self.evaluate(experience_id, policy=policy)
        if not decision.eligible or decision.suggested_status is None:
            return None

        experience = self._experiences.get(experience_id)
        if experience is None:  # pragma: no cover - deleted between the two reads
            raise RecordNotFoundError(f"Experience not found: {experience_id}")

        detail = reason or "; ".join(decision.reasons)
        updated = experience.transition_to(decision.suggested_status, reason=detail)
        return self._experiences.update(updated)
