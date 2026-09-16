"""Milestone 5 acceptance: the three trajectories AER must learn from.

Implements sections 53-55 of the round-5 brief, plus the durability question behind
all of them. The three scenarios are the whole point of the milestone:

1. **Recovery** -- something failed, was repaired, and the task still ended verified
   ==> ``RECOVERY`` with the failed attempts *and* the fix.
2. **False success** -- the agent believed it succeeded while an independent verifier
   found the goal unmet ==> ``FAILURE``. Not success, not discarded.
3. **Unresolved failure** -- the agent failed, tried to repair it, and gave up
   ==> ``FAILURE`` with ``solution=None``. The distiller is not asked to finish a
   story that has no ending.

AER's purpose is not to remember only what worked. It is to remember which
approaches succeeded, which failed, how failures were repaired, and how much
evidence stands behind each of those claims.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import (
    AER,
    ExperienceKind,
    ExperienceStatus,
    HttpStatusVerifier,
    RecordNotFoundError,
)
from tests.experience.support import (
    StaticProvider,
    build_false_success_run,
    build_plain_success_run,
    build_recovery_run,
    build_unresolved_failure_run,
    candidate,
    context_for,
)


def open_runtime(data_dir: Path) -> AER:
    return AER(data_dir, distillation_provider=StaticProvider())


class TestRecoveryExperience:
    def test_a_repaired_task_becomes_a_recovery_experience(self, data_dir: Path) -> None:
        """Section 53: the highest-value trajectory AER produces."""
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer, task="Fix the product page H1")

            assert aer.verified_success(run_id) is True

            experience = aer.distill_run(run_id)
            assert experience is not None

            assert experience.kind is ExperienceKind.RECOVERY
            assert experience.status is ExperienceStatus.VERIFIED
            assert experience.outcome_verified is True
            assert experience.has_verified_solution is True

            # The failed attempts matter as much as the fix (section 29).
            assert experience.failed_attempts
            assert experience.solution
            assert experience.problem
            assert experience.avoid

            # Provenance: this knowledge came from this run.
            assert aer.experience_sources.get_runs(experience.id) == [run_id]
            assert aer.get_experiences_for_run(run_id) == [experience]

    def test_the_experience_carries_the_whole_story(self, data_dir: Path) -> None:
        """The shape section 30 asks for: problem, failed attempt, cause, fix, avoid."""
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert "403" in experience.problem
            assert experience.failed_attempts
            assert experience.root_cause
            assert experience.solution
            assert experience.avoid
            assert experience.symptoms

    def test_it_survives_a_restart(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as first:
            run_id = build_recovery_run(first)
            experience = first.distill_run(run_id)
            assert experience is not None
            experience_id = experience.id

        with open_runtime(data_dir) as second:
            reloaded = second.get_experience(experience_id)
            assert reloaded is not None
            assert reloaded.kind is ExperienceKind.RECOVERY
            assert reloaded.status is ExperienceStatus.VERIFIED
            assert reloaded.outcome_verified is True
            assert reloaded.failed_attempts == experience.failed_attempts
            assert reloaded.solution == experience.solution
            assert second.experience_sources.get_runs(experience_id) == [run_id]
            # And it is still idempotent after the restart.
            assert second.distill_run(run_id) == reloaded  # type: ignore[arg-type]

    def test_a_repaired_run_without_verification_is_still_a_recovery(self, data_dir: Path) -> None:
        """The fix happened; only the trust level differs, and that is what
        ``outcome_verified`` and the status are for."""
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer, verify=False)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.kind is ExperienceKind.RECOVERY
            assert experience.status is ExperienceStatus.DISTILLED
            assert experience.outcome_verified is False
            assert experience.has_verified_solution is False


class TestFalseSuccessExperience:
    """Section 54: the case this whole runtime exists to make visible."""

    def test_a_disagreement_becomes_a_failure(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_id = build_false_success_run(aer)

            run = aer.get_run(run_id)
            assert run is not None
            assert run.status.value == "SUCCESS"
            assert aer.verified_success(run_id) is False

            experience = aer.distill_run(run_id)
            assert experience is not None

            assert experience.kind is ExperienceKind.FAILURE
            # The failure itself is confirmed: an independent check proved the goal
            # was not met. There is still no verified *solution* (section 33).
            assert experience.outcome_verified is True
            assert experience.has_verified_solution is False

    def test_a_lying_provider_cannot_rebrand_it_as_success(self, data_dir: Path) -> None:
        """The provider may be a language model and it may be wrong; the run's
        recorded facts win."""
        provider = StaticProvider(candidate(kind=ExperienceKind.SUCCESS), name="optimist")
        with AER(data_dir, distillation_provider=provider) as aer:
            run_id = build_false_success_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.kind is ExperienceKind.FAILURE
            assert experience.metadata["provider_suggested_kind"] == "SUCCESS"

    def test_the_agent_status_is_not_rewritten(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_id = build_false_success_run(aer)

            before = aer.get_run(run_id)
            aer.distill_run(run_id)

            assert aer.get_run(run_id) == before
            assert before is not None
            assert before.status.value == "SUCCESS"

    def test_it_survives_a_restart(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as first:
            run_id = build_false_success_run(first)
            experience = first.distill_run(run_id)
            assert experience is not None
            experience_id = experience.id

        with open_runtime(data_dir) as second:
            reloaded = second.get_experience(experience_id)
            assert reloaded is not None
            assert reloaded.kind is ExperienceKind.FAILURE
            assert reloaded.status is ExperienceStatus.VERIFIED
            assert reloaded.has_verified_solution is False


class TestUnresolvedFailureExperience:
    """Section 55: legal to persist, incomplete and honest."""

    def test_an_unresolved_failure_is_recorded_without_a_solution(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_id = build_unresolved_failure_run(aer)

            experience = aer.distill_run(run_id)
            assert experience is not None

            assert experience.kind is ExperienceKind.FAILURE
            assert experience.outcome_verified is False
            assert experience.status is ExperienceStatus.DISTILLED
            # No confirmed fix exists, so the field a retrieval pass would read as
            # advice stays empty. The provider's guess is kept as a hypothesis.
            assert experience.solution is None
            assert experience.metadata["provider_solution_hypothesis"] == (
                "switch to a credential with edit_posts"
            )

    def test_the_tried_and_failed_attempts_are_kept(self, data_dir: Path) -> None:
        """Section 4: the value of a failure experience is what did *not* work."""
        with open_runtime(data_dir) as aer:
            run_id = build_unresolved_failure_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.failed_attempts == (
                "blind retry",
                "re-sent the same Authorization header",
            )

    def test_the_runs_own_recorded_failures_fill_in_when_the_provider_is_silent(
        self, data_dir: Path
    ) -> None:
        """The failures were observed, so reporting them is not invention -- and it
        keeps the most valuable field of a failure record from being empty
        (section 29)."""
        provider = StaticProvider(candidate(failed_attempts=(), solution=None))
        with AER(data_dir, distillation_provider=provider) as aer:
            run_id = build_unresolved_failure_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.failed_attempts == (
                "builtins.PermissionError: 403 Forbidden",
                "builtins.RuntimeError: still 403 after clearing the cache",
            )

    def test_the_provider_is_not_asked_to_invent_an_ending(self, data_dir: Path) -> None:
        """A provider that supplies nothing is accepted: no cause and no solution is
        a legitimate answer, and the pipeline does not fill the gap itself."""
        provider = StaticProvider(
            candidate(root_cause=None, solution=None, failed_attempts=("blind retry",))
        )
        with AER(data_dir, distillation_provider=provider) as aer:
            run_id = build_unresolved_failure_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.root_cause is None
            assert experience.solution is None
            assert experience.failed_attempts == ("blind retry",)

    def test_a_failure_cannot_recommend_the_thing_that_failed(self, data_dir: Path) -> None:
        provider = StaticProvider(
            candidate(
                recommended_workflow=("retry the same request",),
                failed_attempts=("retry the same request",),
            )
        )
        with AER(data_dir, distillation_provider=provider) as aer:
            run_id = build_unresolved_failure_run(aer)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.recommended_workflow == ()
            assert experience.metadata["provider_recommended_workflow"] == [
                "retry the same request"
            ]
            assert experience.solution is None
            assert experience.solution is None

    def test_it_survives_a_restart(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as first:
            run_id = build_unresolved_failure_run(first)
            experience = first.distill_run(run_id)
            assert experience is not None
            experience_id = experience.id

        with open_runtime(data_dir) as second:
            reloaded = second.get_experience(experience_id)
            assert reloaded is not None
            assert reloaded.kind is ExperienceKind.FAILURE
            assert reloaded.solution is None
            # The cause is kept -- it is a hypothesis either way, and it is what
            # failure analysis needs most.
            assert reloaded.root_cause == "missing edit_posts capability"
            assert reloaded.failed_attempts == experience.failed_attempts


class TestTheStoreGrowsByEvidenceNotByRows:
    def test_repetition_reinforces_instead_of_duplicating(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_ids = [build_recovery_run(aer, task="the same recurring problem") for _ in range(3)]

            experiences = [aer.distill_run(run_id) for run_id in run_ids]

            assert len({item.id for item in experiences if item is not None}) == 1
            assert aer.experiences.count() == 1
            experience = experiences[0]
            assert experience is not None
            assert sorted(aer.experience_sources.get_runs(experience.id)) == sorted(run_ids)

    def test_nothing_is_distilled_twice(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer)

            for _ in range(3):
                aer.distill_run(run_id)

            assert aer.experiences.count(include_deprecated=True) == 1
            assert len(aer.get_events(run_id)) == 11  # the trace is untouched by re-runs

    def test_an_uneventful_success_is_not_learned_from(self, data_dir: Path) -> None:
        """agent.md #22: generic "normal execution succeeded" records would make
        retrieval worse, not better."""
        with open_runtime(data_dir) as aer:
            run_id = build_plain_success_run(aer)

            assert aer.distill_run(run_id) is None
            assert aer.experiences.count(include_deprecated=True) == 0

    def test_a_failure_and_a_success_about_the_same_words_are_two_records(
        self, data_dir: Path
    ) -> None:
        """The kind is part of the fingerprint: "this did not work" and "this worked"
        are different claims and must not merge."""
        with open_runtime(data_dir) as aer:
            recovered = aer.distill_run(build_recovery_run(aer, task="the same task"))
            failed = aer.distill_run(build_false_success_run(aer, task="the same task"))

            assert recovered is not None and failed is not None
            assert recovered.id != failed.id
            assert {recovered.kind, failed.kind} == {
                ExperienceKind.RECOVERY,
                ExperienceKind.FAILURE,
            }
            assert aer.experiences.count() == 2


class TestBoundaries:
    def test_a_run_that_is_still_going_is_never_distilled(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            context = aer.start_run(task="unfinished", task_type="wordpress")
            with context.tool("wordpress.update_page") as tool:
                tool.set_result({"status": 200})

            assert aer.distill_run(context.run_id) is None
            assert aer.experiences.count(include_deprecated=True) == 0

    def test_verification_is_not_required_to_learn_something(self, data_dir: Path) -> None:
        """A repaired run with no verifier attached is still the most useful kind of
        evidence; the store records the trust level rather than refusing the data."""
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer, verify=False)

            experience = aer.distill_run(run_id)

            assert experience is not None
            assert experience.kind is ExperienceKind.RECOVERY
            assert experience.status is ExperienceStatus.DISTILLED

    def test_distilling_an_unknown_run_raises(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer, pytest.raises(RecordNotFoundError):
            aer.distill_run("no-such-run")

    def test_a_provider_crash_leaves_no_trace_of_an_experience(self, data_dir: Path) -> None:
        from tests.experience.support import CrashingProvider

        with AER(data_dir, distillation_provider=CrashingProvider()) as aer:
            run_id = build_recovery_run(aer)

            with pytest.raises(TimeoutError):
                aer.distill_run(run_id)

            assert aer.experiences.count(include_deprecated=True) == 0
            assert aer.get_experiences_for_run(run_id) == []

    def test_the_database_still_serves_the_milestone_four_questions(self, data_dir: Path) -> None:
        """Experience distillation reads its input but never rewrites it."""
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer)
            before_events = aer.get_events(run_id)
            before_verdicts = aer.get_verifications(run_id)
            before_errors = aer.get_errors(run_id)

            aer.distill_run(run_id)

            assert aer.get_events(run_id) == before_events
            assert aer.get_verifications(run_id) == before_verdicts
            assert aer.get_errors(run_id) == before_errors
            assert aer.verified_success(run_id) is True

    def test_an_http_check_still_works_after_distillation(self, data_dir: Path) -> None:
        with open_runtime(data_dir) as aer:
            run_id = build_recovery_run(aer)
            aer.distill_run(run_id)

            verdict = aer.verify(
                run_id,
                HttpStatusVerifier(expected_status=200, name="post_distill"),
                context=context_for(run_id, actual_status=200),
            )

            assert verdict.passed is True
