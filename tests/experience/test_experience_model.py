"""Experience domain model tests (Milestone 5, sections 5-8 and 46)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aer import Experience, ExperienceKind, ExperienceStatus
from tests.experience.support import candidate


def make_experience(**overrides: object) -> Experience:
    payload: dict[str, object] = {
        "kind": ExperienceKind.SUCCESS,
        "domain": "wordpress",
        "title": "REST API 403 on page update",
        "problem": "Updating a page returns 403",
        "dedup_key": "key",
    }
    payload.update(overrides)
    return Experience(**payload)  # type: ignore[arg-type]


class TestKinds:
    def test_exposes_exactly_three_kinds(self) -> None:
        assert {kind.value for kind in ExperienceKind} == {"SUCCESS", "RECOVERY", "FAILURE"}

    def test_three_kinds_share_one_model(self) -> None:
        """Not three parallel classes: the lifecycle and storage are identical."""
        for kind in ExperienceKind:
            experience = make_experience(kind=kind)
            assert experience.kind is kind
            assert isinstance(experience, Experience)

    def test_an_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_experience(kind="MAYBE")  # type: ignore[arg-type]


class TestFields:
    def test_defaults(self) -> None:
        experience = make_experience()

        assert experience.status is ExperienceStatus.RAW
        assert experience.confidence == 0.0
        assert experience.generalizable is True
        assert experience.outcome_verified is False
        assert experience.symptoms == ()
        assert experience.failed_attempts == ()
        assert experience.recommended_workflow == ()
        assert experience.avoid == ()
        assert experience.metadata == {}
        assert experience.id
        assert experience.created_at.tzinfo is not None
        assert experience.updated_at.tzinfo is not None

    def test_the_dedup_key_is_required(self) -> None:
        """It must be impossible to forget: an experience with no fingerprint
        would silently never match anything."""
        with pytest.raises(ValidationError):
            Experience(  # type: ignore[call-arg]
                kind=ExperienceKind.SUCCESS, domain="d", title="t", problem="p"
            )

    def test_an_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_experience(status="PROVEN_MAYBE")  # type: ignore[arg-type]

    def test_confidence_is_bounded(self) -> None:
        assert make_experience(confidence=0.0).confidence == 0.0
        assert make_experience(confidence=1.0).confidence == 1.0
        with pytest.raises(ValidationError):
            make_experience(confidence=1.5)
        with pytest.raises(ValidationError):
            make_experience(confidence=-0.1)

    def test_lists_are_coerced_into_tuples(self) -> None:
        """Tuples keep an Experience from being edited in place by a caller."""
        experience = make_experience(symptoms=["a", "b"])

        assert experience.symptoms == ("a", "b")
        assert isinstance(experience.symptoms, tuple)

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_experience(typo_field="x")


class TestFailureMayBeIncomplete:
    """Sections 6 and 31: an unresolved failure has no cause and no solution."""

    def test_a_failure_needs_neither_root_cause_nor_solution(self) -> None:
        experience = make_experience(kind=ExperienceKind.FAILURE)

        assert experience.root_cause is None
        assert experience.solution is None

    def test_a_failure_with_failed_attempts_is_valid(self) -> None:
        experience = make_experience(
            kind=ExperienceKind.FAILURE,
            failed_attempts=("blind retry", "changed headers"),
            avoid=("retrying without checking permissions",),
        )

        assert experience.failed_attempts == ("blind retry", "changed headers")
        assert experience.solution is None

    def test_none_is_accepted_explicitly_too(self) -> None:
        experience = make_experience(kind=ExperienceKind.FAILURE, root_cause=None, solution=None)

        assert experience.root_cause is None
        assert experience.solution is None


class TestDerivedViews:
    def test_a_distilled_success_carries_no_verified_solution_yet(self) -> None:
        experience = make_experience(
            status=ExperienceStatus.DISTILLED, solution="switch credential"
        )

        assert experience.has_verified_solution is False

    def test_a_verified_success_carries_a_verified_solution(self) -> None:
        experience = make_experience(
            status=ExperienceStatus.VERIFIED,
            outcome_verified=True,
            solution="switch credential",
        )

        assert experience.has_verified_solution is True

    def test_a_verified_failure_is_not_a_verified_solution(self) -> None:
        """Section 33: "it is confirmed this did not work" is the opposite of a fix."""
        experience = make_experience(
            kind=ExperienceKind.FAILURE,
            status=ExperienceStatus.VERIFIED,
            outcome_verified=True,
            failed_attempts=("blind retry",),
        )

        assert experience.status is ExperienceStatus.VERIFIED
        assert experience.outcome_verified is True
        assert experience.has_verified_solution is False

    def test_a_verified_experience_without_a_solution_has_none_to_verify(self) -> None:
        experience = make_experience(
            status=ExperienceStatus.VERIFIED, outcome_verified=True, solution=None
        )

        assert experience.has_verified_solution is False

    def test_terminal_and_deprecated_track_the_status(self) -> None:
        assert make_experience().is_terminal is False
        assert make_experience().is_deprecated is False

        deprecated = make_experience(status=ExperienceStatus.DEPRECATED)
        assert deprecated.is_terminal is True
        assert deprecated.is_deprecated is True


class TestImmutability:
    def test_status_cannot_be_assigned_around_the_state_machine(self) -> None:
        """Section 10: the lifecycle is deterministic code, not a convention."""
        experience = make_experience()

        with pytest.raises(ValidationError):
            experience.status = ExperienceStatus.PROVEN  # type: ignore[misc]

    def test_other_fields_are_frozen_too(self) -> None:
        experience = make_experience()

        with pytest.raises(ValidationError):
            experience.title = "rewritten"  # type: ignore[misc]

    def test_transitions_return_a_new_object(self) -> None:
        experience = make_experience()

        moved = experience.transition_to(ExperienceStatus.DISTILLED)

        assert moved is not experience
        assert moved.id == experience.id
        assert moved.status is ExperienceStatus.DISTILLED
        # The original is untouched, so a caller cannot be surprised by an action
        # it did not take.
        assert experience.status is ExperienceStatus.RAW


class TestSource:
    def test_links_an_experience_to_a_run(self) -> None:
        from aer import ExperienceSource

        source = ExperienceSource(experience_id="exp-1", run_id="run-1")

        assert source.experience_id == "exp-1"
        assert source.run_id == "run-1"
        assert source.created_at.tzinfo is not None

    def test_is_frozen(self) -> None:
        from aer import ExperienceSource

        source = ExperienceSource(experience_id="exp-1", run_id="run-1")

        with pytest.raises(ValidationError):
            source.run_id = "run-2"  # type: ignore[misc]

    def test_the_candidate_carries_no_id_or_status(self) -> None:
        """A proposal is not a record (section 23)."""
        proposal = candidate()

        assert not hasattr(proposal, "id")
        assert not hasattr(proposal, "status")
