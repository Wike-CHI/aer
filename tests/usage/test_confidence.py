"""Confidence: deterministic, decomposable, and never stored (sections 40-43, 74).

The two properties under test are the ones that make the number trustworthy. It must
be **reproducible** -- the same records and the same clock give the same value to the
bit, with no sampling and no model anywhere -- and it must be **explainable**, which
here means the total is exactly the weighted sum of five components that are each
returned alongside it.

Freshness is the one input that depends on when the question is asked, and the tests
say so explicitly by pinning the clock. That is a property of the metric, not a
weakness: "how recent is this evidence" is not a fact about the evidence alone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aer import (
    AER,
    ConfidenceWeights,
    ExperienceStatus,
    RecordNotFoundError,
    UsageSignal,
    UtilityLabel,
)
from aer.usage.confidence import (
    DEFAULT_CONFIDENCE_WEIGHTS,
    NO_FEEDBACK_PRIOR,
    ExperienceConfidenceService,
)
from tests.knowledge.support import RecordingIndex
from tests.usage.support import (
    indexed_for,
    store_experience,
    use_experience,
    verified_success_run,
)

FIXED_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def pinned(runtime: AER) -> ExperienceConfidenceService:
    """The same computation with the clock held still, so values can be compared."""
    return ExperienceConfidenceService(
        experiences=runtime.experiences,
        effectiveness=runtime.effectiveness,
        clock=lambda: FIXED_NOW,
    )


class TestDeterminism:
    def test_the_same_evidence_gives_the_same_number(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime),
            utility_value=UtilityLabel.HELPFUL,
        )
        service = pinned(usage_runtime)

        first = service.compute("exp-1")
        second = service.compute("exp-1")

        assert first == second
        assert first.confidence == second.confidence

    def test_it_is_reproducible_after_a_restart(self, data_dir, index: RecordingIndex) -> None:
        """Nothing is cached and nothing is stored, so a new process agrees."""
        with AER(data_dir, knowledge_index=index) as first:
            hit = indexed_for(first)
            use_experience(
                first,
                index,
                hit,
                run_id=verified_success_run(first),
                utility_value=UtilityLabel.HELPFUL,
            )
            before = pinned(first).compute("exp-1")

        with AER(data_dir, knowledge_index=RecordingIndex()) as second:
            after = pinned(second).compute("exp-1")

        assert before == after

    def test_computing_does_not_write_anything(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 43: no hidden write-back on every retrieval."""
        hit = indexed_for(usage_runtime)
        use_experience(usage_runtime, index, hit, run_id=verified_success_run(usage_runtime))
        before = usage_runtime.get_experience("exp-1")
        assert before is not None

        usage_runtime.experience_confidence("exp-1")
        usage_runtime.experience_confidence("exp-1")

        after = usage_runtime.get_experience("exp-1")
        assert after == before
        assert after is not None
        assert after.confidence == 0.0


class TestTheComponentsExplainTheNumber:
    def test_the_total_is_the_weighted_sum_of_the_components(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime),
            utility_value=UtilityLabel.HELPFUL,
        )

        confidence = pinned(usage_runtime).compute("exp-1")
        weights = DEFAULT_CONFIDENCE_WEIGHTS

        expected = (
            weights.verification * confidence.verification_component
            + weights.reuse * confidence.reuse_component
            + weights.outcome * confidence.outcome_component
            + weights.feedback * confidence.feedback_component
            + weights.freshness * confidence.freshness_component
        )
        assert confidence.confidence == pytest.approx(expected)
        assert 0.0 <= confidence.confidence <= 1.0

    def test_every_component_is_named_in_the_description(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(usage_runtime, index, hit, run_id=verified_success_run(usage_runtime))

        described = pinned(usage_runtime).compute("exp-1").describe()

        for component in ("verification", "reuse", "outcome", "feedback", "freshness"):
            assert component in described

    def test_no_evidence_at_all_is_reported_as_ungrounded(self, usage_runtime: AER) -> None:
        """An unverified claim nobody has ever retrieved.

        The arithmetic still produces a number -- that is unavoidable and honest --
        and the reader is told it is the floor rather than an assessment.
        """
        store_experience(
            usage_runtime,
            experience_id="exp-quiet",
            status=ExperienceStatus.DISTILLED,
            outcome_verified=False,
        )

        confidence = pinned(usage_runtime).compute("exp-quiet")

        assert confidence.is_grounded is False
        assert confidence.verification_component == 0.0
        assert confidence.reuse_component == 0.0
        assert confidence.outcome_component == 0.0
        assert confidence.feedback_component == NO_FEEDBACK_PRIOR
        # Freshness is the one input that depends on when the question is asked; a
        # claim stored moments ago scores at the top of the decay curve.
        assert confidence.freshness_component == pytest.approx(1.0, abs=0.01)

    def test_a_verified_claim_is_grounded_even_with_no_usage(self, usage_runtime: AER) -> None:
        """Verification is itself evidence, and the only kind a fresh claim has."""
        store_experience(usage_runtime, experience_id="exp-fresh")

        confidence = pinned(usage_runtime).compute("exp-fresh")

        assert confidence.verification_component == 1.0
        assert confidence.is_grounded is True


class TestEachComponentIsWhatItSaysItIs:
    def test_the_verification_component_needs_a_verified_outcome(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        indexed_for(usage_runtime, status=ExperienceStatus.DISTILLED, outcome_verified=False)

        confidence = pinned(usage_runtime).compute("exp-1")

        assert confidence.verification_component == 0.0

    def test_the_reuse_component_counts_distinct_runs_and_saturates(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Popularity must not be able to dominate: five runs is full credit."""
        hit = indexed_for(usage_runtime)
        service = pinned(usage_runtime)

        assert service.compute("exp-1").reuse_component == 0.0

        for position in range(5):
            use_experience(
                usage_runtime,
                index,
                hit,
                run_id=verified_success_run(usage_runtime, run_id=f"run-{position}"),
            )

        confidence = service.compute("exp-1")
        assert confidence.reuse_runs == 5
        assert confidence.reuse_component == 1.0

        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime, run_id="run-6"),
        )
        assert service.compute("exp-1").reuse_component == 1.0

    def test_the_outcome_component_prefers_the_adopted_subset(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        # Adopted into a verified failure: the adopted rate is 0, and the component
        # must not fall back to a more flattering population.
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime, run_id="run-ok"),
            value=None,
        )
        from tests.usage.support import verified_failure_run

        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_failure_run(usage_runtime, run_id="run-bad"),
            value=UsageSignal.ADOPTED,
        )

        report = usage_runtime.experience_effectiveness("exp-1")
        confidence = pinned(usage_runtime).compute("exp-1")

        assert report.injected_verified_success_rate == 0.5
        assert report.adopted_verified_success_rate == 0.0
        assert confidence.outcome_component == 0.0

    def test_a_harmful_report_zeroes_the_feedback_component(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """A confirmed harm is not averaged away by a pile of positive labels."""
        hit = indexed_for(usage_runtime)
        for position in range(3):
            use_experience(
                usage_runtime,
                index,
                hit,
                run_id=verified_success_run(usage_runtime, run_id=f"run-{position}"),
                utility_value=UtilityLabel.HELPFUL,
            )
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime, run_id="run-harm"),
            utility_value=UtilityLabel.HARMFUL,
        )

        confidence = pinned(usage_runtime).compute("exp-1")

        assert confidence.feedback_component == 0.0

    def test_positive_feedback_moves_the_feedback_component(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=verified_success_run(usage_runtime),
            utility_value=UtilityLabel.HELPFUL,
        )

        confidence = pinned(usage_runtime).compute("exp-1")

        assert confidence.feedback_component == 1.0


class TestTheWeightsAreConfiguration:
    def test_weights_that_do_not_sum_to_one_are_refused(self) -> None:
        with pytest.raises(ValueError, match=r"sum to 1\.0"):
            ConfidenceWeights(verification=0.5, reuse=0.5, outcome=0.5)

    def test_a_weight_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"within \[0, 1\]"):
            ConfidenceWeights(verification=-0.1)

    def test_the_defaults_sum_to_one_and_match_the_documented_formula(self) -> None:
        weights = DEFAULT_CONFIDENCE_WEIGHTS

        assert weights.verification == 0.30
        assert weights.reuse == 0.25
        assert weights.outcome == 0.25
        assert weights.feedback == 0.10
        assert weights.freshness == 0.10

    def test_custom_weights_change_the_number_not_the_components(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime)
        use_experience(usage_runtime, index, hit, run_id=verified_success_run(usage_runtime))
        default = pinned(usage_runtime).compute("exp-1")

        reweighted = ExperienceConfidenceService(
            experiences=usage_runtime.experiences,
            effectiveness=usage_runtime.effectiveness,
            weights=ConfidenceWeights(
                verification=0.9, reuse=0.025, outcome=0.025, feedback=0.025, freshness=0.025
            ),
            clock=lambda: FIXED_NOW,
        ).compute("exp-1")

        assert reweighted.verification_component == default.verification_component
        assert reweighted.confidence != default.confidence


class TestBoundaries:
    def test_an_unknown_experience_raises(self, usage_runtime: AER) -> None:
        with pytest.raises(RecordNotFoundError):
            usage_runtime.experience_confidence("no-such-experience")

    def test_a_batch_is_available(self, usage_runtime: AER, index: RecordingIndex) -> None:
        first = indexed_for(usage_runtime, experience_id="exp-1")
        second = indexed_for(usage_runtime, experience_id="exp-2")
        use_experience(
            usage_runtime,
            index,
            first,
            run_id=verified_success_run(usage_runtime, run_id="run-1"),
        )
        use_experience(
            usage_runtime,
            index,
            second,
            run_id=verified_success_run(usage_runtime, run_id="run-2"),
        )

        results = pinned(usage_runtime).compute_many(["exp-1", "exp-2"])

        assert [item.experience_id for item in results] == ["exp-1", "exp-2"]
        assert pinned(usage_runtime).compute_many([]) == []

    def test_a_reuse_scale_of_zero_is_refused(self, usage_runtime: AER) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            ExperienceConfidenceService(
                experiences=usage_runtime.experiences,
                effectiveness=usage_runtime.effectiveness,
                reuse_scale=0,
            )
