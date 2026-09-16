"""The four verifier kinds and the protocol they implement (sections 19-21).

The point of these tests is *interchangeability*: the engine must not care which
kind of verifier it is handed, and the ``verifier_type`` recorded on the verdict
must be the one the verifier declares -- not something the caller passes in.
"""

from __future__ import annotations

import pytest

from aer import (
    CallableEnvironmentVerifier,
    H1CountVerifier,
    HttpStatusVerifier,
    HumanVerifier,
    JsonValidVerifier,
    LLMVerifier,
    PredicateVerifier,
    VerificationContext,
    VerificationError,
    VerificationInputError,
    VerificationResult,
    Verifier,
    VerifierType,
)


def context(**payload: object) -> VerificationContext:
    return VerificationContext(run_id="run-1", payload=payload)  # type: ignore[arg-type]


class TestVerifierTypeVocabulary:
    def test_declaration_order_is_the_trust_order(self) -> None:
        """Most trustworthy first; agent self-evaluation is not a member at all."""
        assert [member.value for member in VerifierType] == [
            "DETERMINISTIC",
            "ENVIRONMENT",
            "HUMAN",
            "LLM",
        ]

    def test_agent_self_evaluation_is_not_a_verification_type(self) -> None:
        assert "AGENT_SELF" not in {member.value for member in VerifierType}
        assert "SELF" not in {member.value for member in VerifierType}


class TestProtocolConformance:
    @pytest.mark.parametrize(
        "verifier",
        [
            HttpStatusVerifier(expected_status=200),
            H1CountVerifier(expected_count=1),
            JsonValidVerifier(),
            PredicateVerifier(name="p", predicate=lambda ctx: True),
            CallableEnvironmentVerifier(name="e", check=lambda ctx: True),
            HumanVerifier(approved=True),
            LLMVerifier(judge=lambda ctx: True),
        ],
    )
    def test_every_built_in_verifier_satisfies_the_protocol(self, verifier: object) -> None:
        assert isinstance(verifier, Verifier)
        assert isinstance(verifier.name, str) and verifier.name
        assert isinstance(verifier.verifier_type, VerifierType)
        assert isinstance(verifier.required, bool)

    def test_a_plain_class_can_satisfy_the_protocol_without_inheriting(self) -> None:
        """Structural typing: no AER import needed to write a verifier."""

        class Custom:
            name = "custom"
            verifier_type = VerifierType.DETERMINISTIC
            required = True

            def verify(self, context: VerificationContext) -> VerificationResult:
                del context
                return VerificationResult(passed=True)

        assert isinstance(Custom(), Verifier)


class TestCallableEnvironmentVerifier:
    def test_is_environment_type(self) -> None:
        verifier = CallableEnvironmentVerifier(name="live_page", check=lambda ctx: True)

        assert verifier.verifier_type is VerifierType.ENVIRONMENT

    def test_runs_the_callable_and_receives_the_context(self) -> None:
        seen: list[VerificationContext] = []

        def check(ctx: VerificationContext) -> VerificationResult:
            seen.append(ctx)
            return VerificationResult(passed=True, result={"status": 200})

        given = context(url="https://example.test/p/1")
        result = CallableEnvironmentVerifier(name="live_page", check=check).verify(given)

        assert seen == [given]
        assert result.passed is True
        assert result.result == {"status": 200}

    def test_a_boolean_callable_is_accepted(self) -> None:
        verifier = CallableEnvironmentVerifier(
            name="live_page",
            check=lambda ctx: ctx.payload.get("reachable") is True,
            message="the live page was unreachable",
        )

        assert verifier.verify(context(reachable=True)).passed is True
        failed = verifier.verify(context(reachable=False))
        assert failed.passed is False
        assert failed.message == "the live page was unreachable"

    def test_an_exception_from_the_callable_propagates(self) -> None:
        def check(ctx: VerificationContext) -> bool:
            del ctx
            raise TimeoutError("the live page took too long")

        verifier = CallableEnvironmentVerifier(name="live_page", check=check)

        with pytest.raises(TimeoutError, match="took too long"):
            verifier.verify(context())


class TestHumanVerifier:
    def test_an_approval_passes(self) -> None:
        verifier = HumanVerifier(approved=True, reviewer="ops-42", comment="looks right")

        result = verifier.verify(context())

        assert result.passed is True
        assert result.result == {"approved": True, "reviewer": "ops-42"}
        assert result.message == "looks right"

    def test_a_rejection_fails(self) -> None:
        verifier = HumanVerifier(approved=False, reviewer="ops-42")

        result = verifier.verify(context())

        assert result.passed is False
        assert result.result["approved"] is False
        assert result.message is not None and "rejected" in result.message

    def test_no_recorded_decision_is_an_input_error_not_a_rejection(self) -> None:
        """Silence is not a negative verdict."""
        verifier = HumanVerifier(approved=None)

        with pytest.raises(VerificationInputError, match="no recorded human decision"):
            verifier.verify(context())

    def test_is_human_type(self) -> None:
        assert HumanVerifier(approved=True).verifier_type is VerifierType.HUMAN

    def test_is_required_by_default(self) -> None:
        assert HumanVerifier(approved=True).required is True

    def test_the_reviewer_identifier_is_stored_verbatim(self) -> None:
        verifier = HumanVerifier(approved=True, reviewer="reviewer-7")

        assert verifier.reviewer == "reviewer-7"


class TestLLMVerifier:
    def test_is_llm_type(self) -> None:
        verifier = LLMVerifier(judge=lambda ctx: True)

        assert verifier.verifier_type is VerifierType.LLM

    def test_is_optional_by_default(self) -> None:
        """An opinion must not be able to veto a task the hard checks confirmed."""
        assert LLMVerifier(judge=lambda ctx: True).required is False

    def test_can_be_made_required_explicitly(self) -> None:
        verifier = LLMVerifier(judge=lambda ctx: True, required=True)

        assert verifier.required is True

    def test_a_judge_returning_a_graded_result_preserves_the_score(self) -> None:
        def judge(ctx: VerificationContext) -> VerificationResult:
            del ctx
            return VerificationResult(passed=False, score=0.4, message="flat and repetitive")

        verifier = LLMVerifier(name="fluency", judge=judge)
        result = verifier.verify(context())

        assert result.passed is False
        assert result.score == 0.4

    def test_the_judge_is_the_caller_supplied_callable(self) -> None:
        judge = lambda ctx: True  # noqa: E731 - the identity of the object is the point

        assert LLMVerifier(judge=judge).judge is judge

    def test_no_provider_is_bound(self) -> None:
        """AER must not import a model SDK to verify something (section 21)."""
        import aer.verification.llm as module

        source = module.__doc__ or ""
        assert "no vendor binding" in source.lower()

        def judge(ctx: VerificationContext) -> bool:
            del ctx
            raise RuntimeError("no model configured")

        with pytest.raises(RuntimeError, match="no model configured"):
            LLMVerifier(judge=judge).verify(context())


class TestVerifierBaseGuards:
    def test_a_missing_verifier_type_is_rejected_at_construction(self) -> None:
        from aer.verification.base import VerifierBase

        class Broken(VerifierBase):
            pass

        with pytest.raises(TypeError, match="must declare verifier_type"):
            Broken("broken")

    def test_an_empty_name_is_rejected(self) -> None:
        from aer.verification.base import VerifierBase

        class Empty(VerifierBase):
            verifier_type = VerifierType.DETERMINISTIC

        with pytest.raises(ValueError, match="non-empty name"):
            Empty("")

    def test_a_non_verifier_object_is_not_confused_for_one(self) -> None:
        assert not isinstance(object(), Verifier)
        assert not isinstance("http_status", Verifier)

    def test_the_normaliser_rejects_an_unusable_outcome(self) -> None:
        from aer.verification.base import as_verification_result

        with pytest.raises(VerificationError, match="must return VerificationResult or bool"):
            as_verification_result(1.0, verifier="score_only")
