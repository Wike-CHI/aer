"""The distillation pass: provider contract, normalisation, validation, crashes.

Sections 20, 23-25, 43 and 50. The theme running through this file is that the
provider is untrusted code whose output is a *proposal*: it may be a language model,
it may lie about the kind, it may return rubbish, and none of that is allowed to
reach storage or to decide what the run was.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aer import (
    AER,
    CallableDistillationProvider,
    CandidateValidationError,
    DistillationError,
    DistillationProvider,
    ExperienceCandidate,
    ExperienceDistiller,
    ExperienceKind,
    ExperienceStatus,
    RunEvidence,
)
from tests.experience.support import (
    CrashingProvider,
    SloppyProvider,
    StaticProvider,
    build_false_success_run,
    build_recovery_run,
    candidate,
)


def distil(runtime: AER, provider: object, run_id: str) -> ExperienceCandidate:
    """Run one distillation pass directly through the public distiller."""
    return ExperienceDistiller(provider).distill(runtime.get_run_evidence(run_id))  # type: ignore[arg-type]


class TestProviderProtocol:
    def test_a_plain_class_satisfies_the_protocol(self) -> None:
        """Structural typing: no AER base class needed to write a provider."""
        assert isinstance(StaticProvider(), DistillationProvider)

    def test_an_object_without_distill_is_rejected(self) -> None:
        assert not isinstance(object(), DistillationProvider)

        with pytest.raises(DistillationError, match="does not implement"):
            ExperienceDistiller(object())  # type: ignore[arg-type]

    def test_a_callable_provider_wraps_a_function(self, aer: AER) -> None:
        seen: list[object] = []

        def rule(evidence: RunEvidence) -> ExperienceCandidate:
            seen.append(evidence)
            return candidate()

        provider = CallableDistillationProvider(rule, name="rule-based")
        run_id = build_recovery_run(aer)
        evidence = aer.get_run_evidence(run_id)

        result = provider.distill(evidence)

        assert provider.name == "rule-based"
        assert seen == [evidence]
        assert isinstance(result, ExperienceCandidate)
        assert "rule-based" in repr(provider)

    def test_a_callable_provider_requires_a_name(self) -> None:
        with pytest.raises(ValueError, match="non-empty name"):
            CallableDistillationProvider(lambda evidence: candidate(), name="")

    def test_the_distiller_exposes_its_provider(self) -> None:
        provider = StaticProvider(name="named")

        distiller = ExperienceDistiller(provider)

        assert distiller.provider is provider
        assert distiller.provider_name == "named"
        assert "named" in repr(distiller)


class TestPass:
    def test_a_candidate_is_produced(self, aer: AER) -> None:
        provider = StaticProvider(candidate(), name="static")
        run_id = build_recovery_run(aer)

        distilled = distil(aer, provider, run_id)

        assert distilled.title == "REST API 403 on page update"
        assert provider.calls

    def test_the_provider_receives_the_complete_evidence(self, aer: AER) -> None:
        provider = StaticProvider(candidate())
        run_id = build_recovery_run(aer)

        distil(aer, provider, run_id)

        seen = provider.calls[0]
        assert isinstance(seen, RunEvidence)
        assert seen.run_id == run_id
        assert len(seen.errors) == 1
        assert len(seen.recoveries) == 1
        assert len(seen.verifications) == 2
        assert seen.verified_success is True

    def test_the_distiller_normalises_the_candidate(self, aer: AER) -> None:
        provider = StaticProvider(
            candidate(
                title="  spaced title  ",
                domain=" wordpress ",
                symptoms=("", "  real symptom  ", "real symptom"),
                solution="   ",
            )
        )
        run_id = build_recovery_run(aer)

        distilled = distil(aer, provider, run_id)

        assert distilled.title == "spaced title"
        assert distilled.domain == "wordpress"
        assert distilled.symptoms == ("real symptom",)
        assert distilled.solution is None

    def test_no_database_is_needed_to_distil(self, aer: AER) -> None:
        """The distiller is storage-agnostic: it only ever sees the package."""
        provider = StaticProvider(candidate())
        run_id = build_recovery_run(aer)
        evidence = aer.get_run_evidence(run_id)

        experience_after = ExperienceDistiller(provider).distill(evidence)

        assert experience_after.kind is None  # provider said nothing
        assert aer.experiences.count(include_deprecated=True) == 0


class TestBadOutput:
    def test_a_non_candidate_return_is_refused(self, aer: AER) -> None:
        run_id = build_recovery_run(aer)

        with pytest.raises(DistillationError, match="must return ExperienceCandidate"):
            distil(aer, SloppyProvider(), run_id)

    def test_a_candidate_without_identity_is_refused(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(title="   ")))
        run_id = build_recovery_run(aer)

        with pytest.raises(CandidateValidationError, match="title is empty"):
            aer.distill_run(run_id)

    def test_nothing_is_persisted_when_the_candidate_is_unusable(
        self, open_aer: Callable[..., AER]
    ) -> None:
        aer = open_aer(StaticProvider(candidate(problem="")))
        run_id = build_recovery_run(aer)

        with pytest.raises(CandidateValidationError):
            aer.distill_run(run_id)

        assert aer.experiences.count(include_deprecated=True) == 0
        assert aer.get_experiences_for_run(run_id) == []

    @pytest.mark.parametrize("missing", ["domain", "title", "problem"])
    def test_every_identity_field_is_checked(
        self, open_aer: Callable[..., AER], missing: str
    ) -> None:
        aer = open_aer(StaticProvider(candidate(**{missing: "  "})))
        run_id = build_recovery_run(aer)

        with pytest.raises(CandidateValidationError, match=f"{missing} is empty"):
            aer.distill_run(run_id)


class TestCrashes:
    def test_the_provider_exception_propagates(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(CrashingProvider(TimeoutError("provider timed out")))
        run_id = build_recovery_run(aer)

        with pytest.raises(TimeoutError, match="provider timed out"):
            aer.distill_run(run_id)

    def test_no_experience_is_created(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(CrashingProvider())
        run_id = build_recovery_run(aer)

        with pytest.raises(TimeoutError):
            aer.distill_run(run_id)

        assert aer.experiences.count(include_deprecated=True) == 0
        assert aer.get_experiences_for_run(run_id) == []

    def test_the_crash_is_recorded_on_the_run(self, open_aer: Callable[..., AER]) -> None:
        """Section 43: distillation is post-processing, so its failure is a system
        observation on the same trace -- never a fabricated FAILURE experience."""
        aer = open_aer(CrashingProvider(name="flaky"))
        run_id = build_recovery_run(aer)
        before = len(aer.get_events(run_id))

        with pytest.raises(TimeoutError):
            aer.distill_run(run_id)

        events = aer.get_events(run_id)
        assert len(events) == before + 1
        assert events[-1].event_type.value == "ERROR"
        assert events[-1].input is not None
        assert events[-1].input["error_type"] == "builtins.TimeoutError"

        recorded = [e for e in aer.get_errors(run_id) if e.metadata.get("source")]
        assert len(recorded) == 1
        assert recorded[0].metadata["source"] == "experience_distiller"
        assert recorded[0].metadata["provider"] == "flaky"
        assert recorded[0].recoverable is False
        assert recorded[0].stack_trace is not None

    def test_the_crash_does_not_touch_the_run_itself(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(CrashingProvider())
        run_id = build_recovery_run(aer)
        before = aer.get_run(run_id)

        with pytest.raises(TimeoutError):
            aer.distill_run(run_id)

        assert aer.get_run(run_id) == before
        assert aer.verified_success(run_id) is True

    def test_an_unconfigured_provider_fails_loudly(self, aer: AER) -> None:
        """A missing provider is a deployment mistake, and must not look like
        "nothing worth learning"."""
        run_id = build_recovery_run(aer)

        assert aer.experience_service.is_configured is False
        with pytest.raises(DistillationError, match="No distillation provider is configured"):
            aer.distill_run(run_id)

    def test_an_unconfigured_provider_leaves_the_trace_untouched(self, aer: AER) -> None:
        """Unlike a crash, nothing unusual happened to the run, and repeating the
        record on every call would bury its real history."""
        run_id = build_recovery_run(aer)
        events_before = aer.get_events(run_id)
        errors_before = aer.get_errors(run_id)

        with pytest.raises(DistillationError):
            aer.distill_run(run_id)

        assert aer.get_events(run_id) == events_before
        assert aer.get_errors(run_id) == errors_before

    def test_the_provider_name_is_reported(self, open_aer: Callable[..., AER]) -> None:
        unconfigured = open_aer()
        assert unconfigured.experience_service.provider_name == "unconfigured"

        configured = open_aer(StaticProvider(name="my-model"))

        assert configured.experience_service.provider_name == "my-model"
        assert configured.experience_service.is_configured is True


class TestKindIsNotTheProvidersToDecide:
    def test_the_system_overrides_a_lying_provider(self, open_aer: Callable[..., AER]) -> None:
        """Section 54: a provider claiming success cannot turn a failed run into one."""
        aer = open_aer(StaticProvider(candidate(kind=ExperienceKind.SUCCESS), name="optimist"))
        run_id = build_recovery_run(aer, verify=False)
        assert aer.verified_success(run_id) is False

        experience = aer.distill_run(run_id)

        assert experience is not None
        # The trajectory contains a successful repair, so the *system* says RECOVERY
        # even though the provider said SUCCESS.
        assert experience.kind is ExperienceKind.RECOVERY
        assert experience.metadata["provider_suggested_kind"] == "SUCCESS"

    def test_an_agreeing_provider_is_not_flagged(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(kind=ExperienceKind.RECOVERY)))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert "provider_suggested_kind" not in experience.metadata

    def test_a_silent_provider_is_fine(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(kind=None)))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.kind is ExperienceKind.RECOVERY


class TestConfidenceBoundary:
    def test_the_hint_never_becomes_the_confidence(self, open_aer: Callable[..., AER]) -> None:
        """Section 38: the final confidence stays 0.0 until reuse data can calibrate
        it; the hint is preserved for the milestone that can."""
        aer = open_aer(StaticProvider(candidate(confidence_hint=0.97)))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.confidence == 0.0
        assert experience.metadata["confidence_hint"] == 0.97

    def test_the_root_cause_confidence_is_kept_as_a_hypothesis_marker(
        self, open_aer: Callable[..., AER]
    ) -> None:
        aer = open_aer(StaticProvider(candidate(root_cause_confidence=0.35)))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.root_cause == "missing edit_posts capability"
        assert experience.metadata["root_cause_confidence"] == 0.35


class TestFailureWorkflowGuard:
    def test_a_failure_may_not_present_a_procedure_as_recommended(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Section 4: what was tried and failed must not be learnable as the fix."""
        aer = open_aer(
            StaticProvider(
                candidate(
                    recommended_workflow=("edit theme.css again",),
                    failed_attempts=("edit theme.css",),
                )
            )
        )
        run_id = build_false_success_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.kind is ExperienceKind.FAILURE
        assert experience.recommended_workflow == ()
        # Nothing is dropped: the suggestions are kept, just not as recommendations.
        assert experience.metadata["provider_recommended_workflow"] == ["edit theme.css again"]

    def test_a_failure_keeps_its_avoid_list(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(avoid=("blind retry",))))
        run_id = build_false_success_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.avoid == ("blind retry",)

    def test_a_success_may_recommend_a_procedure(self, open_aer: Callable[..., AER]) -> None:
        """The guard applies to failures only: a repair that worked *is* a procedure."""
        aer = open_aer(StaticProvider(candidate()))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.recommended_workflow == ("check the capability before retrying",)
        assert "provider_recommended_workflow" not in experience.metadata


class TestFailedAttemptsFallback:
    def test_recorded_errors_fill_in_when_the_provider_says_nothing(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Section 29: the failed attempts are the most valuable part of a recovery
        experience, and the run already recorded them."""
        aer = open_aer(StaticProvider(candidate(failed_attempts=())))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.kind is ExperienceKind.RECOVERY
        assert experience.failed_attempts == ("builtins.PermissionError: 403 Forbidden",)

    def test_the_providers_attempts_win_when_present(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(failed_attempts=("blind retry",))))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.failed_attempts == ("blind retry",)

    def test_no_fallback_is_invented_for_a_success(self, open_aer: Callable[..., AER]) -> None:
        """A plain success has no failed attempts, and none are made up."""
        from tests.experience.support import build_plain_success_run

        aer = open_aer(StaticProvider(candidate(failed_attempts=())))
        run_id = build_plain_success_run(aer)

        experience = aer.distill_run(run_id, explicit_high_value=True)

        assert experience is not None
        assert experience.kind is ExperienceKind.SUCCESS
        assert experience.failed_attempts == ()


class TestStoredProvenance:
    def test_the_provider_and_policy_are_recorded(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(name="my-model"))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        distillation = experience.metadata["distillation"]
        assert isinstance(distillation, dict)
        assert distillation["provider"] == "my-model"
        assert distillation["policy"] == "default"
        assert "ERROR" in distillation["triggers"]
        assert distillation["reasons"]

    def test_the_lifecycle_reason_is_recorded(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.status is ExperienceStatus.VERIFIED
        transition = experience.metadata["last_transition"]
        assert isinstance(transition, dict)
        assert transition["from"] == "DISTILLED"
        assert transition["to"] == "VERIFIED"
        assert transition["reason"]

    def test_the_candidate_metadata_is_carried_over(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider(candidate(metadata={"model": "gpt-x", "tokens": 812})))
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert experience.metadata["model"] == "gpt-x"
        assert experience.metadata["tokens"] == 812
