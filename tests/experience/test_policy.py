"""Distillation trigger policy (Milestone 5, sections 15-19 and 48).

The policy decides *whether* a run is worth a distillation pass and never what the
experience says. Each test here states one trigger and asserts both the decision and
the reason attached to it, because a boolean nobody can explain is not auditable.
"""

from __future__ import annotations

from aer import (
    AER,
    DistillationTrigger,
    ExperienceKind,
    HttpStatusVerifier,
    RunStatus,
    VerificationContext,
)
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


class TestInconclusiveOutcomes:
    """Round-8.1.1 section 4: what AER may learn from a run nobody described.

    ``INCONCLUSIVE`` is not a sixth flavour of failure. It is the absence of a claim,
    and the only thing that can replace a claim is evidence -- so these tests are about
    the boundary between "unknown" and "established", not about severity.
    """

    @staticmethod
    def _inconclusive(aer: AER, *, verified: bool) -> str:
        """A run that ended without a declaration, optionally independently confirmed."""
        context = aer.start_run(task="a session that ended without an outcome")
        context.tool("wordpress.update_page")
        if verified:
            context.verify(
                HttpStatusVerifier(expected_status=200, required=True),
                context=VerificationContext(run_id=context.run_id, payload={"actual_status": 200}),
            )
        context.finish(RunStatus.INCONCLUSIVE)
        return context.run_id

    def test_nothing_is_learned_from_silence(self, aer: AER) -> None:
        """No declaration, no verdict, no kind. The veto is the correct answer."""
        run_id = self._inconclusive(aer, verified=False)

        decision = aer.evaluate_distillation(run_id)

        assert decision.should_distill is False
        assert decision.kind is None
        assert decision.triggers == ()
        assert "no outcome was declared" in decision.reasons[0]

    def test_a_recorded_error_does_not_rescue_it(self, aer: AER) -> None:
        """The distinction that matters: an error inside the run is not a task outcome.

        A ``FAILURE`` experience claims the goal was not met. An error proves a step did
        not work, and for a run whose ending nobody described that is not the same
        statement -- so the run is vetoed even though ``ERROR`` would normally trigger
        distillation (D-100).
        """
        context = aer.start_run(task="a session that ended without an outcome")
        try:
            with context.tool("wordpress.update_page"):
                raise RuntimeError("the update failed")
        except RuntimeError:
            pass
        context.finish(RunStatus.INCONCLUSIVE)

        decision = aer.evaluate_distillation(context.run_id)

        assert decision.should_distill is False
        assert decision.kind is None

    def test_explicit_high_value_cannot_force_a_kind_out_of_nothing(self, aer: AER) -> None:
        """There is no kind to record, so asking harder does not produce one."""
        run_id = self._inconclusive(aer, verified=False)

        decision = aer.evaluate_distillation(run_id, explicit_high_value=True)

        assert decision.should_distill is False
        assert decision.kind is None

    def test_independent_evidence_makes_it_a_verified_success(self, aer: AER) -> None:
        """The point of the whole status: proof replaces the missing declaration.

        Note where the run is compared: against a *declared* success, not against a
        failure. Once a required check has passed, an ``INCONCLUSIVE`` run is a verified
        success in the same sense a ``SUCCESS`` run is -- which is what makes the
        status worth having at all.
        """
        run_id = self._inconclusive(aer, verified=True)

        assert aer.verified_success(run_id) is True

        decision = aer.evaluate_distillation(run_id)

        # And from there the ordinary policy applies unchanged: an uneventful verified
        # success teaches nothing, exactly as it does when the agent declared one.
        assert decision.should_distill is False
        assert decision.kind is None
        assert "plain verified success" in decision.reasons[0]

    def test_an_eventful_run_becomes_distillable_once_evidence_settles_it(self, aer: AER) -> None:
        """Same trajectory, different outcome: what the verification changed.

        The recorded error is enough to make the run interesting; it is the *kind* that
        needed the evidence. Without it the run above is vetoed, with it the run is a
        ``SUCCESS`` whose errors are recorded as triggers -- and notably still not a
        ``FAILURE``, because a successful run may contain failed steps.
        """
        run = aer.start_run(task="a session that ended without an outcome")
        attempt = run.tool("wordpress.update_page")
        try:
            with attempt:
                raise PermissionError("403 Forbidden")
        except PermissionError:
            pass
        run.verify(
            HttpStatusVerifier(expected_status=200, required=True),
            context=VerificationContext(run_id=run.run_id, payload={"actual_status": 200}),
        )
        run.finish(RunStatus.INCONCLUSIVE)

        decision = aer.evaluate_distillation(run.run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.SUCCESS
        assert DistillationTrigger.ERROR in decision.triggers

    def test_a_repaired_run_becomes_a_recovery(self, aer: AER) -> None:
        """The highest-value kind is reachable without a declaration too."""
        run = aer.start_run(task="a session that ended without an outcome")
        attempt = run.tool("wordpress.update_page")
        try:
            with attempt:
                raise PermissionError("403 Forbidden")
        except PermissionError:
            pass
        error_id = attempt.error_record.id
        with run.recovery(reason="REST API 403", error_id=error_id) as recovery:
            recovery.set_result({"action": "granted_edit_posts"})
        run.verify(
            HttpStatusVerifier(expected_status=200, required=True),
            context=VerificationContext(run_id=run.run_id, payload={"actual_status": 200}),
        )
        run.finish(RunStatus.INCONCLUSIVE)

        decision = aer.evaluate_distillation(run.run_id)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.RECOVERY

    def test_the_old_statuses_still_read_as_failures(self, aer: AER) -> None:
        """Regression: nothing about ``FAILED`` / ``ABORTED`` / ``PARTIAL_SUCCESS`` moved."""
        for finish, expected in (
            ("fail", RunStatus.FAILED),
            ("abort", RunStatus.ABORTED),
            ("partial_success", RunStatus.PARTIAL_SUCCESS),
        ):
            context = aer.start_run(task=f"{finish} run")
            getattr(context, finish)()

            decision = aer.evaluate_distillation(context.run_id)

            assert context.status is expected
            assert decision.should_distill is True
            assert decision.kind is ExperienceKind.FAILURE
            assert DistillationTrigger.FAILED_RUN in decision.triggers


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
