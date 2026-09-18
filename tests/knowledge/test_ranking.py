"""Scoring, pinned against what the engine actually returned.

The numbers in the first test are not invented: ``-2.03`` and ``-0.47`` are values
read out of neug 0.2.0 during the capability probe, for a strong and a weak match on
the same query. If the engine ever changes convention, this file is where it should
be noticed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aer.knowledge import ranking
from aer.runtime.enums import ExperienceStatus

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

STRONG_MATCH = -2.03
WEAK_MATCH = -0.47


class TestTextScore:
    """BM25 arrives backwards and negative. Only this function knows that."""

    def test_a_strong_match_scores_higher_than_a_weak_one(self) -> None:
        assert ranking.text_score(STRONG_MATCH) > ranking.text_score(WEAK_MATCH)

    def test_scores_are_positive_and_bounded(self) -> None:
        for raw in (STRONG_MATCH, WEAK_MATCH, -100.0, 0.0):
            assert 0.0 <= ranking.text_score(raw) < 1.0

    def test_no_relevance_is_zero(self) -> None:
        assert ranking.text_score(0.0) == 0.0

    def test_magnitudes_are_preserved_in_order(self) -> None:
        scores = [ranking.text_score(value) for value in (-4.0, -2.0, -1.0, -0.1)]
        assert scores == sorted(scores, reverse=True)

    def test_a_positive_score_does_not_invert_the_ranking(self) -> None:
        """If the engine ever flips convention, the failure must be visible.

        Silently negating would rank the best match last, which is far harder to
        spot in a result set than everything collapsing to the same value.
        """
        assert ranking.text_score(2.0) == 0.0

    def test_the_documented_example_from_the_engine(self) -> None:
        """``1 / (1 + |bm25|)`` -- written down so the curve is not a mystery."""
        assert ranking.text_score(-1.0) == pytest.approx(0.5)
        assert ranking.text_score(-3.0) == pytest.approx(0.75)


class TestTrustScore:
    """How much the record vouches for itself. Never ``confidence``."""

    def test_verified_and_confirmed_is_full_trust(self) -> None:
        assert ranking.trust_score(ExperienceStatus.VERIFIED, outcome_verified=True) == 1.0

    def test_an_unconfirmed_outcome_earns_half_credit(self) -> None:
        assert ranking.trust_score(ExperienceStatus.VERIFIED, outcome_verified=False) == 0.5

    def test_distilled_is_worth_less_than_verified(self) -> None:
        assert ranking.trust_score(
            ExperienceStatus.DISTILLED, outcome_verified=True
        ) < ranking.trust_score(ExperienceStatus.VERIFIED, outcome_verified=True)

    def test_raw_is_worth_less_than_distilled(self) -> None:
        assert ranking.trust_score(
            ExperienceStatus.RAW, outcome_verified=True
        ) < ranking.trust_score(ExperienceStatus.DISTILLED, outcome_verified=True)

    def test_everything_at_or_above_verified_scores_the_same(self) -> None:
        """The differences between them come from usage data that does not exist yet.

        Inventing a gradient now would bake a guess into the ranking that later has
        to be un-learned.
        """
        scores = {
            ranking.trust_score(status, outcome_verified=True)
            for status in (
                ExperienceStatus.VERIFIED,
                ExperienceStatus.REUSED,
                ExperienceStatus.PROVEN,
                ExperienceStatus.TRAINING_CANDIDATE,
                ExperienceStatus.TRAINING_DATA,
            )
        }
        assert scores == {1.0}

    def test_deprecated_earns_nothing(self) -> None:
        assert ranking.trust_score(ExperienceStatus.DEPRECATED, outcome_verified=True) == 0.0


class TestDomainAndFreshness:
    def test_an_exact_domain_match_scores(self) -> None:
        assert ranking.domain_score("wordpress", "wordpress") == 1.0

    def test_a_different_domain_scores_nothing(self) -> None:
        assert ranking.domain_score("seo", "wordpress") == 0.0

    def test_an_unspecified_domain_imposes_no_signal(self) -> None:
        """Not a penalty, and not a bonus for whichever domain happens to sort first."""
        assert ranking.domain_score("seo", None) == 0.0
        assert ranking.domain_score("wordpress", None) == 0.0

    def test_freshness_halves_after_the_half_life(self) -> None:
        assert ranking.freshness_score(
            NOW - timedelta(days=ranking.FRESHNESS_HALF_LIFE_DAYS), now=NOW
        ) == pytest.approx(0.5)

    def test_a_brand_new_experience_is_fresh(self) -> None:
        assert ranking.freshness_score(NOW, now=NOW) == 1.0

    def test_a_future_timestamp_does_not_overshoot(self) -> None:
        assert ranking.freshness_score(NOW + timedelta(days=5), now=NOW) == 1.0

    def test_freshness_decays_monotonically(self) -> None:
        scores = [
            ranking.freshness_score(NOW - timedelta(days=days), now=NOW)
            for days in (0, 30, 180, 365, 1000)
        ]
        assert scores == sorted(scores, reverse=True)


class TestCombine:
    """The shape of the formula is the guarantee, so it is tested as one."""

    def test_bonuses_cannot_overturn_a_clear_relevance_gap(self) -> None:
        """The property section 41 needs: relevance is the primary dimension.

        A much better text match with *no* supporting signals still beats a much
        worse match with every bonus maxed out.
        """
        best_text = ranking.text_score(-5.0)
        worst_text = ranking.text_score(-0.1)
        assert best_text > worst_text * ranking.MAX_BONUS_FACTOR

        strong = ranking.combine(text=best_text, trust=0.0, domain=0.0, freshness=0.0)
        weak = ranking.combine(text=worst_text, trust=1.0, domain=1.0, freshness=1.0)
        assert strong > weak

    def test_trust_reorders_near_equal_relevance(self) -> None:
        """The other half of the promise: trust decides between similar matches."""
        verified = ranking.combine(
            text=ranking.text_score(-1.0), trust=1.0, domain=1.0, freshness=1.0
        )
        unverified = ranking.combine(
            text=ranking.text_score(-1.1), trust=0.0, domain=0.0, freshness=0.0
        )
        assert verified > unverified

    def test_identical_inputs_score_identically(self) -> None:
        arguments = {"text": 0.4, "trust": 0.5, "domain": 1.0, "freshness": 0.25}
        assert ranking.combine(**arguments) == ranking.combine(**arguments)

    def test_the_maximum_multiplier_is_the_documented_bound(self) -> None:
        score = ranking.combine(text=1.0, trust=1.0, domain=1.0, freshness=1.0)
        assert score == pytest.approx(ranking.MAX_BONUS_FACTOR)

    def test_confidence_is_not_an_input(self) -> None:
        """Stated as a test because "let us also use confidence" is the obvious
        next change, and it must wait until something calibrates it (D-037)."""
        import inspect

        assert "confidence" not in inspect.signature(ranking.combine).parameters
