"""Confidence: a number you can take apart.

The design constraint is that this must never become a mysterious composite score
(round-7 brief, sections 40-43). So confidence here is **a weighted sum of five
components, each of which is a fact about recorded evidence**, and the components are
returned alongside the total so that any number can be explained without a rerun.

    confidence = 0.30 * verification + 0.25 * reuse + 0.25 * outcome
               + 0.10 * feedback    + 0.10 * freshness

What each component is, and what it deliberately is not:

* **verification** -- binary. ``1.0`` when the claim is at or beyond ``VERIFIED`` and
  its outcome is evidence-backed, ``0.0`` otherwise. A verified claim is a fact about
  the record, not a matter of degree;
* **reuse** -- distinct runs the experience was injected into, saturating at
  :data:`DEFAULT_REUSE_SCALE`. Saturation is not a detail: without it, one very
  frequently retrieved experience would dominate every comparison, and popularity is
  not correctness;
* **outcome** -- the adopted verified success rate when there is one, falling back to
  the injected rate, and ``0.0`` when neither exists. Note what this is *not*: it is
  not a causal effect (section 30), it is the observed rate among runs that saw the
  experience;
* **feedback** -- explicit utility labels. ``0.0`` if anything was reported harmful (a
  single confirmed harm is not averaged away), ``1.0``-scaled by the helpful fraction
  otherwise, and :data:`NO_FEEDBACK_PRIOR` when nobody said anything, so silence
  neither rewards nor punishes;
* **freshness** -- exponential decay with the same half-life the ranker uses. Guidance
  about a REST API does not rot, so this is weak by construction.

Two properties are guaranteed and tested:

* **deterministic** -- the same records and the same clock produce the same number, to
  the bit. No model, no sampling, no LLM anywhere in this path (section 74);
* **not stored** -- :meth:`ExperienceConfidenceService.compute` never writes. A stored
  ``confidence`` that nothing maintains goes stale exactly like the counters
  ``docs/DECISIONS.md`` D-037 refused to add, so the value is recomputed on demand.
  For the same reason this does not trigger a knowledge projection: the projection
  carries what retrieval needs, and confidence is not part of it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from aer.exceptions import RecordNotFoundError
from aer.knowledge.ranking import freshness_score
from aer.runtime.enums import ExperienceStatus
from aer.runtime.lifecycle import is_at_least
from aer.runtime.models import Experience
from aer.runtime.serialization import utc_now
from aer.storage.repositories import ExperienceRepository
from aer.usage.effectiveness import ExperienceEffectivenessReport, ExperienceEffectivenessService

__all__ = [
    "DEFAULT_CONFIDENCE_WEIGHTS",
    "DEFAULT_REUSE_SCALE",
    "NO_FEEDBACK_PRIOR",
    "ConfidenceWeights",
    "ExperienceConfidence",
    "ExperienceConfidenceService",
]

#: Number of distinct reuse runs at which the reuse component saturates.
DEFAULT_REUSE_SCALE = 5

#: What "nobody said anything about its usefulness" contributes.
#:
#: The midpoint, because an absence of feedback is not evidence in either direction.
#: Zero would make every experience nobody commented on look worse than one with a
#: single lukewarm review, which is a bias dressed up as rigour.
NO_FEEDBACK_PRIOR = 0.5


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    """The weights, as configuration rather than as constants in a formula.

    Validated on construction so a misconfigured set fails at the point it is
    written rather than silently producing a confidence outside ``[0, 1]``.
    """

    verification: float = 0.30
    reuse: float = 0.25
    outcome: float = 0.25
    feedback: float = 0.10
    freshness: float = 0.10

    def __post_init__(self) -> None:
        for name in ("verification", "reuse", "outcome", "feedback", "freshness"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"weight {name}={value} must be within [0, 1]")
        total = self.verification + self.reuse + self.outcome + self.feedback + self.freshness
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"confidence weights must sum to 1.0, got {total!r}. A set that does "
                "not sum to one would let confidence leave [0, 1]."
            )


#: The default weights. Section 42 fixes the shape of the formula; these are the
#: numbers, and they are under test so a change is a deliberate edit.
DEFAULT_CONFIDENCE_WEIGHTS = ConfidenceWeights()


@dataclass(frozen=True, slots=True)
class ExperienceConfidence:
    """A confidence value that can be taken apart.

    The components are the interesting part; ``confidence`` is only their weighted
    sum. Anything that shows the number should be able to show the breakdown, which
    is why they are all returned rather than kept internal.
    """

    experience_id: str
    confidence: float

    verification_component: float
    reuse_component: float
    outcome_component: float
    feedback_component: float
    freshness_component: float

    reuse_runs: int
    """The raw count behind :attr:`reuse_component`."""

    age_days: float

    @property
    def is_grounded(self) -> bool:
        """Whether any evidence at all stands behind this number.

        An experience with no verification, no reuse and no outcome still has a
        confidence -- the arithmetic is well defined -- but a reader should be able to
        tell that it is the floor value rather than an assessment.
        """
        return (
            self.verification_component > 0.0
            or self.reuse_component > 0.0
            or self.outcome_component > 0.0
        )

    def describe(self) -> str:
        """A one-line breakdown, for a log or a status command."""
        return (
            f"{self.experience_id}: confidence {self.confidence:.3f} "
            f"(verification {self.verification_component:.2f}, "
            f"reuse {self.reuse_component:.2f} over {self.reuse_runs} run(s), "
            f"outcome {self.outcome_component:.2f}, "
            f"feedback {self.feedback_component:.2f}, "
            f"freshness {self.freshness_component:.2f}, "
            f"age {self.age_days:.0f}d)"
        )


class ExperienceConfidenceService:
    """Computes confidence from stored evidence, on demand, deterministically."""

    def __init__(
        self,
        *,
        experiences: ExperienceRepository,
        effectiveness: ExperienceEffectivenessService,
        weights: ConfidenceWeights | None = None,
        reuse_scale: int = DEFAULT_REUSE_SCALE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if reuse_scale < 1:
            raise ValueError(f"reuse_scale={reuse_scale} must be at least 1")
        self._experiences = experiences
        self._effectiveness = effectiveness
        self._weights = weights or DEFAULT_CONFIDENCE_WEIGHTS
        self._reuse_scale = float(reuse_scale)
        self._clock = clock if callable(clock) else utc_now

    @property
    def weights(self) -> ConfidenceWeights:
        """The weights in force for this service."""
        return self._weights

    def compute(self, experience_id: str) -> ExperienceConfidence:
        """Confidence for one experience, recomputed from scratch.

        Raises:
            RecordNotFoundError: no such experience exists.
        """
        return self.compute_many([experience_id])[0]

    def compute_many(self, experience_ids: Sequence[str]) -> list[ExperienceConfidence]:
        """Confidence for several experiences, sharing one set of queries.

        Raises:
            RecordNotFoundError: any of the ids does not exist.
        """
        unique = list(dict.fromkeys(experience_ids))
        if not unique:
            return []

        known = self._experiences.get_many(unique)
        missing = [experience_id for experience_id in unique if experience_id not in known]
        if missing:
            raise RecordNotFoundError(f"Experience(s) not found: {missing}")

        reports = {report.experience_id: report for report in self._effectiveness.reports(unique)}
        now = self._clock()
        return [
            self._compose(known[experience_id], reports[experience_id], now)
            for experience_id in unique
        ]

    # -- internals ---------------------------------------------------------

    def _compose(
        self,
        experience: Experience,
        report: ExperienceEffectivenessReport,
        now: datetime,
    ) -> ExperienceConfidence:
        """Assemble the five components and their weighted sum."""
        verification = (
            1.0
            if is_at_least(experience.status, ExperienceStatus.VERIFIED)
            and experience.outcome_verified
            else 0.0
        )
        reuse = min(1.0, report.distinct_target_runs / self._reuse_scale)
        outcome = _outcome_component(report)
        feedback = _feedback_component(report)

        age_days = max(0.0, (now - experience.updated_at).total_seconds() / 86_400.0)
        freshness = freshness_score(experience.updated_at, now=now)

        weights = self._weights
        confidence = (
            weights.verification * verification
            + weights.reuse * reuse
            + weights.outcome * outcome
            + weights.feedback * feedback
            + weights.freshness * freshness
        )
        return ExperienceConfidence(
            experience_id=experience.id,
            confidence=confidence,
            verification_component=verification,
            reuse_component=reuse,
            outcome_component=outcome,
            feedback_component=feedback,
            freshness_component=freshness,
            reuse_runs=report.distinct_target_runs,
            age_days=age_days,
        )


def _outcome_component(report: ExperienceEffectivenessReport) -> float:
    """How well it did where it was used, or ``0.0`` when that is unknown.

    Prefers the adopted subset -- runs where an agent demonstrably used it -- over
    the injected population, because that is the narrower and more meaningful
    denominator. Falls back rather than averaging the two: a single blended number
    would hide which population produced it.
    """
    if report.adopted_verified_success_rate is not None:
        return report.adopted_verified_success_rate
    if report.injected_verified_success_rate is not None:
        return report.injected_verified_success_rate
    return 0.0


def _feedback_component(report: ExperienceEffectivenessReport) -> float:
    """The explicit-utility component.

    A reported harm zeroes the component outright: harmful feedback is a claim that
    the experience actively misled someone, and averaging it against a pile of
    positive labels would let volume bury it.
    """
    if report.harmful_count > 0:
        return 0.0
    considered = report.helpful_count + report.neutral_count
    if considered == 0:
        return NO_FEEDBACK_PRIOR
    return report.helpful_count / considered
