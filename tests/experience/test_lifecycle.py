"""The Experience lifecycle state machine (Milestone 5, sections 9-10, 34-36, 47).

The ordering under test is ``RAW -> DISTILLED -> VERIFIED``, not the
``RAW -> VERIFIED -> DISTILLED`` that ``agent.md``, ``docs/TASKS.md`` and the design
document originally declared. Both the code and those documents were corrected in
this milestone (``docs/DECISIONS.md`` D-029); the reasoning is restated in the last
test of this file so the change cannot be reverted by accident.
"""

from __future__ import annotations

import pytest

from aer import (
    ALLOWED_TRANSITIONS,
    Experience,
    ExperienceKind,
    ExperienceLifecycleError,
    ExperienceStatus,
    allowed_transitions_from,
    can_transition,
    is_terminal,
    reachable_from,
)


def make_experience(status: ExperienceStatus = ExperienceStatus.RAW) -> Experience:
    return Experience(
        kind=ExperienceKind.SUCCESS,
        domain="wordpress",
        title="title",
        problem="problem",
        dedup_key="key",
        solution="the fix",
        status=status,
    )


class TestDeclaration:
    def test_every_status_has_a_row_in_the_table(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(ExperienceStatus)

    def test_statuses_are_declared_in_lifecycle_order(self) -> None:
        assert [status.value for status in ExperienceStatus] == [
            "RAW",
            "DISTILLED",
            "VERIFIED",
            "REUSED",
            "PROVEN",
            "TRAINING_CANDIDATE",
            "TRAINING_DATA",
            "DEPRECATED",
        ]


class TestLegalTransitions:
    def test_the_happy_path(self) -> None:
        experience = make_experience()

        distilled = experience.transition_to(ExperienceStatus.DISTILLED)
        verified = distilled.mark_verified(reason="2 required verifications passed")

        assert distilled.status is ExperienceStatus.DISTILLED
        assert verified.status is ExperienceStatus.VERIFIED
        assert verified.outcome_verified is True

    def test_anything_can_be_deprecated(self) -> None:
        for status in ExperienceStatus:
            if status is ExperienceStatus.DEPRECATED:
                continue
            assert can_transition(status, ExperienceStatus.DEPRECATED), status

    def test_deprecation_records_why(self) -> None:
        deprecated = make_experience().transition_to(
            ExperienceStatus.DEPRECATED, reason="superseded by a better fix"
        )

        transition = deprecated.metadata["last_transition"]
        assert isinstance(transition, dict)
        assert transition["from"] == "RAW"
        assert transition["to"] == "DEPRECATED"
        assert transition["reason"] == "superseded by a better fix"
        assert transition["at"]


class TestForbiddenTransitions:
    @pytest.mark.parametrize(
        ("start", "target"),
        [
            ("RAW", "VERIFIED"),
            ("RAW", "REUSED"),
            ("RAW", "PROVEN"),
            ("RAW", "TRAINING_CANDIDATE"),
            ("RAW", "TRAINING_DATA"),
            ("DISTILLED", "REUSED"),
            ("DISTILLED", "PROVEN"),
            ("VERIFIED", "TRAINING_CANDIDATE"),
            ("VERIFIED", "DISTILLED"),
            ("VERIFIED", "RAW"),
        ],
    )
    def test_a_jump_over_a_stage_is_refused(self, start: str, target: str) -> None:
        experience = make_experience(ExperienceStatus(start))

        with pytest.raises(ExperienceLifecycleError, match="is not an allowed"):
            experience.transition_to(ExperienceStatus(target))

    def test_promotion_beyond_verified_is_impossible_today(self) -> None:
        """Section 56: reuse, proven-ness and training candidacy need data that does
        not exist yet, so no run of the pipeline may reach them."""
        for start in (ExperienceStatus.RAW, ExperienceStatus.DISTILLED, ExperienceStatus.VERIFIED):
            reachable = reachable_from(start)
            assert ExperienceStatus.REUSED not in reachable
            assert ExperienceStatus.PROVEN not in reachable
            assert ExperienceStatus.TRAINING_CANDIDATE not in reachable
            assert ExperienceStatus.TRAINING_DATA not in reachable

    def test_reaching_the_same_status_is_refused(self) -> None:
        """A no-op transition means the caller misunderstood something."""
        experience = make_experience()

        with pytest.raises(ExperienceLifecycleError, match="already RAW"):
            experience.transition_to(ExperienceStatus.RAW)

    def test_marking_verified_twice_is_refused(self) -> None:
        verified = make_experience(ExperienceStatus.DISTILLED).mark_verified()

        with pytest.raises(ExperienceLifecycleError, match="already VERIFIED"):
            verified.mark_verified()


class TestTerminalState:
    def test_deprecated_is_terminal(self) -> None:
        assert is_terminal(ExperienceStatus.DEPRECATED) is True
        assert allowed_transitions_from(ExperienceStatus.DEPRECATED) == frozenset()

    def test_nothing_leaves_deprecated(self) -> None:
        deprecated = make_experience().transition_to(ExperienceStatus.DEPRECATED)

        for target in ExperienceStatus:
            with pytest.raises(ExperienceLifecycleError):
                deprecated.transition_to(target)

    def test_deprecation_is_idempotent_by_refusal(self) -> None:
        """A second withdrawal must fail loudly, not silently succeed."""
        deprecated = make_experience().transition_to(ExperienceStatus.DEPRECATED)

        with pytest.raises(ExperienceLifecycleError, match="already DEPRECATED"):
            deprecated.transition_to(ExperienceStatus.DEPRECATED)


class TestTransitionBookkeeping:
    def test_updated_at_moves_but_created_at_does_not(self) -> None:
        experience = make_experience()

        moved = experience.transition_to(ExperienceStatus.DISTILLED)

        assert moved.created_at == experience.created_at
        assert moved.updated_at >= experience.updated_at

    def test_metadata_is_preserved_and_extended(self) -> None:
        experience = Experience(
            kind=ExperienceKind.SUCCESS,
            domain="wordpress",
            title="title",
            problem="problem",
            dedup_key="key",
            solution="the fix",
            metadata={"kept": "yes"},
        )

        moved = experience.transition_to(ExperienceStatus.DISTILLED)

        assert moved.metadata["kept"] == "yes"
        assert "last_transition" in moved.metadata

    def test_mark_verified_only_changes_the_status_and_the_flag(self) -> None:
        experience = make_experience(ExperienceStatus.DISTILLED)

        verified = experience.mark_verified(reason="why not")

        assert verified.outcome_verified is True
        assert verified.title == experience.title
        assert verified.solution == experience.solution
        assert verified.dedup_key == experience.dedup_key


class TestLifecycleSemantics:
    def test_distillation_precedes_verification(self) -> None:
        """D-029, restated as a test.

        ``RAW -> VERIFIED -> DISTILLED`` (the original wording in agent.md #24 and
        TASKS.md Task 5.2) is impossible: verification is a check *of a claim*, and a
        claim only exists once the trajectory has been distilled. The documents were
        corrected rather than the code being bent to match them.
        """
        assert can_transition(ExperienceStatus.RAW, ExperienceStatus.DISTILLED)
        assert can_transition(ExperienceStatus.DISTILLED, ExperienceStatus.VERIFIED)
        assert not can_transition(ExperienceStatus.RAW, ExperienceStatus.VERIFIED)
        assert not can_transition(ExperienceStatus.VERIFIED, ExperienceStatus.DISTILLED)

        with pytest.raises(ExperienceLifecycleError):
            make_experience().transition_to(ExperienceStatus.VERIFIED)
