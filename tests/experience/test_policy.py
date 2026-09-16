"""Distillation trigger policy (Milestone 5, sections 15-19 and 48).

The policy decides *whether* a run is worth a distillation pass and never what the
experience says. Each test here states one trigger and asserts both the decision and
the reason attached to it, because a boolean nobody can explain is not auditable.
"""

from __future__ import annotations

from aer import AER, DistillationTrigger, ExperienceKind, HttpStatusVerifier, RunStatus
from tests.experience.support import (
    add_human_feedback,
    build_false_success_run,
    build_plain_success_run,
    build_recovery_run,
    build_unresolved_failure_run,
    context_for,
)


class TestPlainSuccessIsIgnored:
    def test_a_verified_uneventful_success_is_not_distilled(self, aer: AER) -> None:
        """Section 17 and agent.md #22: no errors, no repair, no human input, and it
        worked. Generic "normal execution succeeded" records make retrieval worse."""
        run_id = build_plain_success_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is False
        assert decision.kind is None
        assert decision.triggers == ()
        assert decision.is_veto is True
        assert "plain verified success" in decision.reasons[0]

    def test_explicit_high_value_keeps_it_anyway(self, aer: AER) -> None:
        run_id = build_plain_success_run(aer)

        decision = aer.evaluate_distillation(run_id, explicit_high_value=True)

        assert decision.should_distill is True
        assert DistillationTrigger.EXPLICIT_HIGH_VALUE in decision.triggers
        assert decision.kind is ExperienceKind.SUCCESS

    def test_an_unfinished_run_is_vetoed(self, aer: AER) -> None:
        """There is no outcome yet, so there is nothing to learn from (and guessing
        one would store a verdict about a situation still in progress)."""
        context = aer.start_run(task="still going", task_type="wordpress")
        context.tool("wordpress.update_page")

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.should_distill is False
        assert decision.kind is None
        assert "still RUNNING" in decision.reasons[0]


class TestRecoveryIsThePriority:
    def test_error_plus_successful_recovery_plus_verification(self, aer: AER) -> None:
        """Section 18: the highest-value trigger AER has."""
        run_id = build_recovery_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.RECOVERY
        assert DistillationTrigger.ERROR in decision.triggers
        assert DistillationTrigger.RECOVERY in decision.triggers

    def test_recovery_without_verification_is_still_a_recovery(self, aer: AER) -> None:
        """The fix happened; discarding it because nobody checked would throw away
        the most valuable kind of evidence (section 2)."""
        run_id = build_recovery_run(aer, task="unverified repair", verify=False)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.RECOVERY


class TestFailures:
    def test_a_failed_run_is_a_failure(self, aer: AER) -> None:
        run_id = build_unresolved_failure_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.FAILURE
        assert DistillationTrigger.ERROR in decision.triggers
        assert DistillationTrigger.RECOVERY in decision.triggers
        assert DistillationTrigger.FAILED_RUN in decision.triggers

    def test_a_bare_failure_with_no_evidence_at_all_is_still_kept(self, aer: AER) -> None:
        """A run can end with no error row and no verdict -- the agent simply gives
        up -- and that is exactly what a failure experience is for."""
        context = aer.start_run(task="gave up immediately", task_type="wordpress")
        context.fail()

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.FAILURE
        assert decision.triggers == (DistillationTrigger.FAILED_RUN,)

    def test_false_success_is_a_failure(self, aer: AER) -> None:
        """Section 19: the agent misjudged its own success. The most valuable data
        AER produces about agent judgement, and it must not be filed as success."""
        run_id = build_false_success_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.FAILURE
        assert DistillationTrigger.VERIFICATION_FAILURE in decision.triggers

    def test_an_error_without_recovery_is_a_failure(self, aer: AER) -> None:
        context = aer.start_run(task="error then gave up", task_type="wordpress")
        attempt = context.tool("wordpress.update_page")
        try:
            with attempt:
                raise PermissionError("403 Forbidden")
        except PermissionError:
            pass
        context.fail()

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.FAILURE


class TestOtherTriggers:
    def test_human_feedback_triggers_distillation(self, aer: AER) -> None:
        """Section 16. A person spent attention on this run, so it is worth keeping."""
        run_id = build_plain_success_run(aer, task="reviewed by a human")

        add_human_feedback(aer, run_id)
        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert DistillationTrigger.HUMAN_FEEDBACK in decision.triggers
        assert decision.kind is ExperienceKind.SUCCESS

    def test_an_unverified_success_is_kept(self, aer: AER) -> None:
        """Section 16's "agent says success but verified_success is false".

        Note the kind: unverified is *not* failure (nothing showed it failed), so the
        missing trust is carried by ``outcome_verified`` and by the lifecycle stopping
        at DISTILLED instead of reaching VERIFIED (D-031).
        """
        context = aer.start_run(task="agent says done", task_type="wordpress")
        with context.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        context.success()

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.SUCCESS
        assert DistillationTrigger.UNVERIFIED_SUCCESS in decision.triggers

    def test_a_passing_optional_verification_does_not_hide_unverified_success(
        self, aer: AER
    ) -> None:
        context = aer.start_run(task="only an optional check ran", task_type="wordpress")
        with context.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        context.success()
        context.verify(
            HttpStatusVerifier(expected_status=200, required=False),
            context=context_for(context.run_id, actual_status=200),
        )

        decision = aer.evaluate_distillation(context.run_id)

        assert DistillationTrigger.UNVERIFIED_SUCCESS in decision.triggers


class TestDecisionObject:
    def test_reasons_explain_every_trigger(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert len(decision.reasons) >= len(decision.triggers)
        assert all(reason for reason in decision.reasons)

    def test_the_decision_is_frozen_and_carries_the_run_id(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.run_id == run_id
        assert "should_distill=True" in repr(decision)

    def test_evaluating_does_not_require_a_provider(self, aer: AER) -> None:
        """The dry run is a pure read: no provider, no writes."""
        run_id = build_recovery_run(aer)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is True
        assert aer.experiences.count() == 0

    def test_statuses_that_are_not_success_all_read_as_failed_runs(self, aer: AER) -> None:
        for finish, status in (("fail", RunStatus.FAILED), ("abort", RunStatus.ABORTED)):
            context = aer.start_run(task=f"{finish} run", task_type="wordpress")
            getattr(context, finish)()

            decision = aer.evaluate_distillation(context.run_id)

            assert decision.kind is ExperienceKind.FAILURE
            assert context.status is status

    def test_partial_success_is_a_failure_not_a_success(self, aer: AER) -> None:
        """A partly done task is a form of "not done"; there is no fourth kind."""
        context = aer.start_run(task="half done", task_type="wordpress")
        context.partial_success()

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.kind is ExperienceKind.FAILURE
