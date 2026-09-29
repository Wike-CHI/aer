"""Promotion policy: when a verified claim earns ``REUSED`` and ``PROVEN`` (sections 33-39, 71-73).

The two bars are deliberately far apart, and most of these tests are about the lower
one being strict in one specific way and the upper one being strict in several:

* ``REUSED`` needs an injection into a run that is **not the one the experience came
  from**. The source run is the one case where relevance is guaranteed, so it cannot
  count as evidence that the knowledge generalises;
* ``PROVEN`` needs explicit adoption signals from several distinct runs, most of them
  verified successes, and a clean feedback record. Exposure -- however much of it --
  is not allowed to substitute for any of that.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    ExperienceStatus,
    PromotionPolicy,
    RecordNotFoundError,
    UsageSignal,
    UtilityLabel,
)
from tests.knowledge.support import RecordingIndex
from tests.usage.support import (
    DEFAULT_QUERY,
    indexed_for,
    retrieve,
    use_experience,
    verified_failure_run,
    verified_success_run,
)


def adopt(
    runtime: AER,
    index: RecordingIndex,
    hit: object,
    *,
    run_id: str,
    success: bool = True,
    adopted: bool = True,
) -> None:
    """One run that used the experience, ending the way the caller asked.

    Creates the run first so the outcome is a real one from ``runs`` and
    ``verifications``: the report reads those tables, and a hand-made verdict would
    make every promotion test prove something about the test.
    """
    run = (
        verified_success_run(runtime, run_id=run_id)
        if success
        else verified_failure_run(runtime, run_id=run_id)
    )
    use_experience(
        runtime,
        index,
        hit,
        run_id=run,
        value=UsageSignal.ADOPTED if adopted else None,
    )


class TestPromotionToReused:
    def test_injection_into_another_run_earns_reused(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 33: the minimal, honest evidence that knowledge was reused."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        target = verified_success_run(usage_runtime, run_id="run-target")

        use_experience(usage_runtime, index, hit, run_id=target)

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is True
        assert decision.suggested_status is ExperienceStatus.REUSED
        promoted = usage_runtime.promote_experience("exp-1")
        assert promoted is not None
        assert promoted.status is ExperienceStatus.REUSED

    def test_injection_into_its_own_source_run_is_not_reuse(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 34: the run that produced it is where it is trivially relevant."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)

        use_experience(usage_runtime, index, hit, run_id=source)

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert "source run" in " ".join(decision.reasons)
        assert usage_runtime.promote_experience("exp-1") is None
        stored = usage_runtime.get_experience("exp-1")
        assert stored is not None
        assert stored.status is ExperienceStatus.VERIFIED

    def test_retrieval_alone_is_not_reuse(self, usage_runtime: AER, index: RecordingIndex) -> None:
        """Section 33: being retrieved is not being used."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        target = verified_success_run(usage_runtime, run_id="run-target")

        retrieve(usage_runtime, index, guidance=[hit], run_id=target, query=DEFAULT_QUERY)

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert "never been injected" in " ".join(decision.reasons)

    def test_reuse_does_not_require_the_second_run_to_succeed(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 35: being used again is the fact. Being used *well* is not the bar."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        failure = verified_failure_run(usage_runtime, run_id="run-target")

        use_experience(usage_runtime, index, hit, run_id=failure)

        promoted = usage_runtime.promote_experience("exp-1")
        assert promoted is not None
        assert promoted.status is ExperienceStatus.REUSED

    def test_an_untracked_run_cannot_earn_reuse(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """An injected row with no target run is exposure without attribution."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)

        use_experience(usage_runtime, index, hit, run_id=None)

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False

    def test_a_claim_below_verified_is_not_promoted(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        hit = indexed_for(usage_runtime, status=ExperienceStatus.DISTILLED, outcome_verified=False)
        target = verified_success_run(usage_runtime, run_id="run-target")

        use_experience(usage_runtime, index, hit, run_id=target)

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert "below VERIFIED" in " ".join(decision.reasons)

    def test_a_withdrawn_experience_is_never_promoted(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        target = verified_success_run(usage_runtime, run_id="run-target")
        use_experience(usage_runtime, index, hit, run_id=target)

        experience = usage_runtime.get_experience("exp-1")
        assert experience is not None
        usage_runtime.experiences.update(
            experience.transition_to(ExperienceStatus.DEPRECATED, reason="superseded")
        )

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert "withdrawn" in " ".join(decision.reasons)


class TestPromotionToProven:
    def test_adoption_across_enough_verified_runs_earns_proven(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """The full ladder: ``VERIFIED -> REUSED -> PROVEN``, one step at a time."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(5):
            adopt(usage_runtime, index, hit, run_id=f"run-{position}")

        assert usage_runtime.promote_experience("exp-1") is not None
        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is True
        assert decision.suggested_status is ExperienceStatus.PROVEN
        proven = usage_runtime.promote_experience("exp-1")
        assert proven is not None
        assert proven.status is ExperienceStatus.PROVEN

    def test_one_run_short_is_not_proven(self, usage_runtime: AER, index: RecordingIndex) -> None:
        """Four distinct adopted runs is one below the configured minimum."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(4):
            adopt(usage_runtime, index, hit, run_id=f"run-{position}")
        usage_runtime.promote_experience("exp-1")

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert any("distinct run" in reason for reason in decision.reasons)
        assert usage_runtime.promote_experience("exp-1") is None

    def test_enough_runs_but_not_enough_verified_successes(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Five adopted runs, only three of them verified successes."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(3):
            adopt(usage_runtime, index, hit, run_id=f"run-ok-{position}", success=True)
        for position in range(2):
            adopt(usage_runtime, index, hit, run_id=f"run-bad-{position}", success=False)
        usage_runtime.promote_experience("exp-1")

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.adopted_runs == 5
        assert report.adopted_verified_success_runs == 3

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert any("verified-successful" in reason for reason in decision.reasons)

    def test_exposure_without_adoption_never_reaches_proven(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 38: a hundred injections with no signal stay at ``REUSED``.

        The system does not know whether anyone read the experience, so it refuses to
        call it proven -- the strongest statement of the milestone is the one that
        needs the strongest evidence.
        """
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(6):
            adopt(
                usage_runtime,
                index,
                hit,
                run_id=f"run-{position}",
                adopted=False,
            )

        assert usage_runtime.promote_experience("exp-1") is not None
        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert any("no adoption signal" in reason for reason in decision.reasons)

    def test_harmful_feedback_blocks_proven(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 73: a high success rate does not cancel a confirmed harm."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(5):
            adopt(usage_runtime, index, hit, run_id=f"run-{position}")
        # One more exposure judged harmful, with everything else still excellent.
        harm = verified_success_run(usage_runtime, run_id="run-harm")
        use_experience(
            usage_runtime,
            index,
            hit,
            run_id=harm,
            value=UsageSignal.ADOPTED,
            utility_value=UtilityLabel.HARMFUL,
        )
        usage_runtime.promote_experience("exp-1")

        report = usage_runtime.experience_effectiveness("exp-1")
        assert report.harmful_count == 1
        assert report.adopted_verified_success_rate == 1.0

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert any("harmful" in reason for reason in decision.reasons)

    def test_adopted_into_a_failure_is_not_proven(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 70 at the lifecycle level: adoption is not correctness."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(4):
            adopt(usage_runtime, index, hit, run_id=f"run-bad-{position}", success=False)
        adopt(usage_runtime, index, hit, run_id="run-ok", success=True)
        usage_runtime.promote_experience("exp-1")

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert any("success rate" in reason for reason in decision.reasons)

    def test_a_proven_experience_has_nothing_left_to_do(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        for position in range(5):
            adopt(usage_runtime, index, hit, run_id=f"run-{position}")
        usage_runtime.promote_experience("exp-1")
        usage_runtime.promote_experience("exp-1")

        decision = usage_runtime.evaluate_promotion("exp-1")
        assert decision.eligible is False
        assert decision.is_noop is True
        assert usage_runtime.promote_experience("exp-1") is None


class TestPromotionPolicyIsConfiguration:
    def test_a_lower_threshold_promotes_sooner(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 37: the numbers are configuration, not doctrine.

        Two adopted runs, one of them a verified success: below the default bar for
        ``PROVEN`` and above a deliberately lowered one.
        """
        policy = PromotionPolicy(
            minimum_adopted_runs=2,
            minimum_adopted_verified_successes=1,
            minimum_adopted_success_rate=0.5,
        )
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        adopt(usage_runtime, index, hit, run_id="run-a")
        adopt(usage_runtime, index, hit, run_id="run-b", success=False)
        usage_runtime.promote_experience("exp-1")

        assert usage_runtime.evaluate_promotion("exp-1").eligible is False
        decision = usage_runtime.evaluate_promotion("exp-1", policy=policy)
        assert decision.eligible is True
        assert decision.suggested_status is ExperienceStatus.PROVEN
        assert usage_runtime.promote_experience("exp-1", policy=policy) is not None

    def test_thresholds_are_validated(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            PromotionPolicy(minimum_adopted_runs=0)
        with pytest.raises(ValueError, match="cannot exceed"):
            PromotionPolicy(minimum_adopted_runs=2, minimum_adopted_verified_successes=3)
        with pytest.raises(ValueError, match=r"within \[0, 1\]"):
            PromotionPolicy(minimum_adopted_success_rate=1.5)

    def test_the_default_policy_matches_the_documented_numbers(self) -> None:
        """Pinned so that changing a threshold is a deliberate edit, not a drift."""
        from aer import DEFAULT_PROMOTION_POLICY

        assert DEFAULT_PROMOTION_POLICY.minimum_adopted_runs == 5
        assert DEFAULT_PROMOTION_POLICY.minimum_adopted_verified_successes == 4
        assert DEFAULT_PROMOTION_POLICY.minimum_adopted_success_rate == 0.80


class TestPromotionWritesAndProjects:
    def test_the_promotion_is_committed_with_a_reason(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        adopt(usage_runtime, index, hit, run_id="run-target")

        usage_runtime.promote_experience("exp-1")

        stored = usage_runtime.get_experience("exp-1")
        assert stored is not None
        transition = stored.metadata["last_transition"]
        assert isinstance(transition, dict)
        assert transition["from"] == "VERIFIED"
        assert transition["to"] == "REUSED"
        assert transition["reason"]

    def test_the_claim_itself_is_not_rewritten(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        adopt(usage_runtime, index, hit, run_id="run-target")
        before = usage_runtime.get_experience("exp-1")
        assert before is not None

        promoted = usage_runtime.promote_experience("exp-1")

        assert promoted is not None
        assert promoted.solution == before.solution
        assert promoted.problem == before.problem
        assert promoted.dedup_key == before.dedup_key
        assert promoted.outcome_verified is True

    def test_the_knowledge_index_is_updated_after_the_commit(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """Section 44: the status change must reach what retrieval says about it."""
        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        adopt(usage_runtime, index, hit, run_id="run-target")

        usage_runtime.promote_experience("exp-1")

        projected = [
            experience.status
            for experiences, _ in index.upserts
            for experience in experiences
            if experience.id == "exp-1"
        ]
        assert "REUSED" in projected

    def test_a_projection_failure_does_not_undo_the_commit(
        self, usage_runtime: AER, index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section 44: SQLite is the fact; a stale index is repaired by a rebuild."""
        from aer.exceptions import ProjectionError

        source = verified_success_run(usage_runtime, run_id="run-source")
        hit = indexed_for(usage_runtime, source_run_id=source)
        adopt(usage_runtime, index, hit, run_id="run-target")

        def explode(experience_id: str):
            raise ProjectionError("the index is unavailable")

        monkeypatch.setattr(usage_runtime, "project_experience", explode)

        promoted = usage_runtime.promote_experience("exp-1")

        assert promoted is not None
        assert promoted.status is ExperienceStatus.REUSED
        stored = usage_runtime.get_experience("exp-1")
        assert stored is not None
        assert stored.status is ExperienceStatus.REUSED

    def test_an_unknown_experience_raises(self, usage_runtime: AER) -> None:
        with pytest.raises(RecordNotFoundError):
            usage_runtime.evaluate_promotion("no-such-experience")
        with pytest.raises(RecordNotFoundError):
            usage_runtime.promote_experience("no-such-experience")

    def test_the_decision_explains_itself_either_way(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """An operator asking "why is this not proven?" must get an answer."""
        hit = indexed_for(usage_runtime)
        adopt(usage_runtime, index, hit, run_id="run-target")

        decision = usage_runtime.evaluate_promotion("exp-1")

        assert decision.experience_id == "exp-1"
        assert decision.reasons
        assert decision.describe().startswith("exp-1:")
