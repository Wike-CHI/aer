"""Deterministic verifier tests (Milestone 4, round-4 brief sections 15-18, 38).

These run the verifiers directly, without a runtime, because a primitive's
contract should be pinnable on its own: given this context, expect exactly this
verdict. A predicate that is only ever exercised through ``run.verify()`` hides
its own behaviour behind a database.
"""

from __future__ import annotations

import pytest

from aer import (
    H1CountVerifier,
    HttpStatusVerifier,
    JsonValidVerifier,
    PredicateVerifier,
    VerificationContext,
    VerificationError,
    VerificationInputError,
    VerificationResult,
    VerifierType,
)


def context(**payload: object) -> VerificationContext:
    """A context carrying ``payload`` as the evidence under test."""
    return VerificationContext(run_id="run-1", payload=payload)  # type: ignore[arg-type]


class TestHttpStatusVerifier:
    def test_matching_status_passes(self) -> None:
        verifier = HttpStatusVerifier(expected_status=200)

        result = verifier.verify(context(actual_status=200))

        assert result.passed is True
        assert result.result == {"actual_status": 200, "expected_status": 200}
        assert result.message is not None and "200" in result.message

    def test_mismatched_status_fails(self) -> None:
        verifier = HttpStatusVerifier(expected_status=200)

        result = verifier.verify(context(actual_status=403))

        assert result.passed is False
        assert result.result == {"actual_status": 403, "expected_status": 200}

    def test_is_deterministic(self) -> None:
        assert HttpStatusVerifier(expected_status=200).verifier_type is (VerifierType.DETERMINISTIC)

    def test_default_name(self) -> None:
        assert HttpStatusVerifier(expected_status=200).name == "http_status"

    def test_a_custom_payload_key_is_honoured(self) -> None:
        verifier = HttpStatusVerifier(expected_status=200, payload_key="status_code")

        assert verifier.verify(context(status_code=200)).passed is True

    def test_missing_evidence_is_an_input_error_not_a_failure(self) -> None:
        """Missing evidence is not negative evidence."""
        verifier = HttpStatusVerifier(expected_status=200)

        with pytest.raises(VerificationInputError, match="actual_status"):
            verifier.verify(context())

    def test_a_boolean_is_not_accepted_as_a_status_code(self) -> None:
        """``True`` is an ``int`` in Python; silently reading it as 1 would hide a bug."""
        verifier = HttpStatusVerifier(expected_status=1)

        with pytest.raises(VerificationInputError, match="integer"):
            verifier.verify(context(actual_status=True))

    def test_a_string_status_is_rejected(self) -> None:
        verifier = HttpStatusVerifier(expected_status=200)

        with pytest.raises(VerificationInputError, match="integer"):
            verifier.verify(context(actual_status="200"))

    def test_no_score_is_invented_for_a_boolean_check(self) -> None:
        """Deterministic pass/fail produces no graded quality; ``None`` says so."""
        verifier = HttpStatusVerifier(expected_status=200)

        assert verifier.verify(context(actual_status=200)).score is None
        assert verifier.verify(context(actual_status=500)).score is None


class TestH1CountVerifier:
    def test_exactly_one_h1_passes(self) -> None:
        verifier = H1CountVerifier(expected_count=1)

        result = verifier.verify(context(actual_count=1))

        assert result.passed is True
        assert result.result == {"actual_count": 1, "expected_count": 1}

    def test_zero_h1_fails(self) -> None:
        """The canonical "agent said it updated the heading" disagreement."""
        verifier = H1CountVerifier(expected_count=1)

        result = verifier.verify(context(actual_count=0))

        assert result.passed is False
        assert result.result == {"actual_count": 0, "expected_count": 1}
        assert result.message is not None and "0 <h1>" in result.message

    def test_multiple_h1_fails(self) -> None:
        verifier = H1CountVerifier(expected_count=1)

        assert verifier.verify(context(actual_count=3)).passed is False

    def test_is_deterministic(self) -> None:
        assert H1CountVerifier(expected_count=1).verifier_type is VerifierType.DETERMINISTIC

    def test_missing_count_is_an_input_error(self) -> None:
        with pytest.raises(VerificationInputError, match="actual_count"):
            H1CountVerifier(expected_count=1).verify(context())


class TestJsonValidVerifier:
    @pytest.mark.parametrize(
        "document",
        ['{"a": 1}', "[1, 2, 3]", '"text"', "42", "null"],
    )
    def test_valid_documents_pass(self, document: str) -> None:
        verifier = JsonValidVerifier()

        result = verifier.verify(context(document=document))

        assert result.passed is True
        assert result.result["valid"] is True

    @pytest.mark.parametrize("document", ["{not json}", "", '{"a": }'])
    def test_invalid_documents_fail_with_a_reason(self, document: str) -> None:
        verifier = JsonValidVerifier()

        result = verifier.verify(context(document=document))

        # A malformed document is a *verdict*, not a crash: the verifier ran fine.
        assert result.passed is False
        assert result.result["valid"] is False
        assert result.result["error"]

    def test_a_non_string_document_is_an_input_error(self) -> None:
        with pytest.raises(VerificationInputError, match="string"):
            JsonValidVerifier().verify(context(document={"already": "parsed"}))

    def test_missing_document_is_an_input_error(self) -> None:
        with pytest.raises(VerificationInputError, match="document"):
            JsonValidVerifier().verify(context())


class TestPredicateVerifier:
    def test_a_true_predicate_passes_silently(self) -> None:
        verifier = PredicateVerifier(
            name="has_title",
            predicate=lambda ctx: bool(ctx.payload.get("title")),
            message="the page needs a title",
        )

        result = verifier.verify(context(title="Hello"))

        assert result.passed is True
        # A passing check needs no apology; the fallback message is for failures.
        assert result.message is None

    def test_a_false_predicate_explains_itself(self) -> None:
        verifier = PredicateVerifier(
            name="has_title",
            predicate=lambda ctx: bool(ctx.payload.get("title")),
            message="the page needs a title",
        )

        result = verifier.verify(context())

        assert result.passed is False
        assert result.message == "the page needs a title"

    def test_a_predicate_may_return_a_full_result(self) -> None:
        detail = VerificationResult(
            passed=False,
            score=0.25,
            message="only 1 of 4 meta tags present",
            result={"present": 1, "required": 4},
        )
        verifier = PredicateVerifier(name="meta_tags", predicate=lambda ctx: detail)

        result = verifier.verify(context())

        assert result is detail
        assert result.score == 0.25

    def test_a_predicate_returning_nonsense_is_a_verifier_error(self) -> None:
        verifier = PredicateVerifier(name="bad", predicate=lambda ctx: "yes")

        with pytest.raises(VerificationError, match="must return VerificationResult or bool"):
            verifier.verify(context())

    def test_receives_the_context_it_was_given(self) -> None:
        seen: list[VerificationContext] = []

        def predicate(ctx: VerificationContext) -> bool:
            seen.append(ctx)
            return True

        given = context(answer=42)
        PredicateVerifier(name="spy", predicate=predicate).verify(given)

        assert seen == [given]

    def test_is_deterministic_and_named_by_the_caller(self) -> None:
        verifier = PredicateVerifier(name="h1_count", predicate=lambda ctx: True)

        assert verifier.name == "h1_count"
        assert verifier.verifier_type is VerifierType.DETERMINISTIC
