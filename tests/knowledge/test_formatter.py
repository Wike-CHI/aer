"""Labels and the rendered context.

Section 73 of the round-6 brief asks for the four labels to be pinned, plus the
rule that a failure never carries a solution. The rest of this file checks the two
things that make the output safe to put in a prompt: it says what it is, and it
contains nothing from the raw trace.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aer.knowledge.formatter import (
    DEFAULT_MAX_CHARS,
    PREAMBLE,
    TRUNCATION_MARKER,
    ExperienceContextFormatter,
)
from aer.knowledge.models import (
    KNOWN_FAILURE_LABEL,
    RetrievalHit,
    RetrievalResult,
)
from aer.runtime.enums import ExperienceKind, ExperienceStatus, RetrievalMode

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def hit(
    *,
    kind: ExperienceKind = ExperienceKind.RECOVERY,
    status: ExperienceStatus = ExperienceStatus.VERIFIED,
    outcome_verified: bool = True,
    title: str = "WordPress REST API 403",
    problem: str = "WordPress REST API 403 应用密码无效",
    root_cause: str | None = "应用密码缺少 edit_posts",
    solution: str | None = "改用有 edit_posts 权限的应用密码",
    failed_attempts: tuple[str, ...] = ("盲目重试",),
    avoid: tuple[str, ...] = ("不要盲目重试",),
    experience_id: str = "exp-1",
    bm25_score: float = -2.0,
) -> RetrievalHit:
    """A scored hit, with the numbers kept deliberately boring."""
    return RetrievalHit(
        experience_id=experience_id,
        kind=kind,
        status=status,
        domain="wordpress",
        title=title,
        problem=problem,
        root_cause=root_cause,
        solution=solution,
        failed_attempts=failed_attempts,
        avoid=avoid,
        outcome_verified=outcome_verified,
        generalizable=True,
        source_count=2,
        created_at=NOW,
        updated_at=NOW,
        bm25_score=bm25_score,
        text_score=0.66,
        trust_score=1.0,
        domain_score=1.0,
        freshness_score=1.0,
        retrieval_score=1.0,
    )


def result(*hits: RetrievalHit, guidance_count: int | None = None) -> RetrievalResult:
    """A result whose first ``guidance_count`` hits are guidance."""
    split = len(hits) if guidance_count is None else guidance_count
    return RetrievalResult(
        query="WordPress REST API 403",
        domain="wordpress",
        mode=RetrievalMode.GUIDANCE,
        guidance=tuple(hits[:split]),
        warnings=tuple(hits[split:]),
    )


class TestLabels:
    """Section 73: the four roles, and nothing in between."""

    def test_a_verified_success_is_a_verified_success(self) -> None:
        assert hit(kind=ExperienceKind.SUCCESS).label == "Verified Success"

    def test_a_verified_recovery_is_a_verified_recovery(self) -> None:
        assert hit(kind=ExperienceKind.RECOVERY).label == "Verified Recovery"

    def test_a_failure_is_a_known_failure(self) -> None:
        assert (
            hit(kind=ExperienceKind.FAILURE, solution=None, outcome_verified=True).label
            == KNOWN_FAILURE_LABEL
        )

    def test_an_unverified_recovery_says_so(self) -> None:
        assert (
            hit(status=ExperienceStatus.DISTILLED, outcome_verified=False).label
            == "Unverified Recovery Observation"
        )

    def test_an_unverified_success_says_so(self) -> None:
        assert (
            hit(
                kind=ExperienceKind.SUCCESS,
                status=ExperienceStatus.DISTILLED,
                outcome_verified=False,
            ).label
            == "Unverified Success Observation"
        )

    def test_verified_status_with_an_unconfirmed_outcome_is_not_verified(self) -> None:
        """Two independent conditions, both required."""
        assert (
            hit(status=ExperienceStatus.VERIFIED, outcome_verified=False).label
            == "Unverified Recovery Observation"
        )

    def test_distilled_status_is_not_verified_even_with_a_confirmed_outcome(self) -> None:
        assert (
            hit(status=ExperienceStatus.DISTILLED, outcome_verified=True).label
            == "Unverified Recovery Observation"
        )

    def test_deprecation_overrides_everything(self) -> None:
        """A withdrawn solution is not a verified solution."""
        assert hit(status=ExperienceStatus.DEPRECATED).label == "Deprecated"
        assert (
            hit(kind=ExperienceKind.FAILURE, status=ExperienceStatus.DEPRECATED).label
            == "Deprecated"
        )


class TestRendering:
    """What the blocks actually say."""

    def test_a_recovery_block_has_the_expected_sections(self) -> None:
        rendered = ExperienceContextFormatter().format(result(hit()))
        assert "[Verified Recovery]" in rendered
        assert "Problem: " in rendered
        assert "Failed attempts:" in rendered
        assert "Recovery: " in rendered
        assert "Avoid:" in rendered

    def test_a_success_block_calls_the_solution_proven(self) -> None:
        rendered = ExperienceContextFormatter().format(
            result(hit(kind=ExperienceKind.SUCCESS, failed_attempts=()))
        )
        assert "[Verified Success]" in rendered
        assert "Proven approach: " in rendered

    def test_a_failure_block_has_no_solution_line(self) -> None:
        """The single most damaging mistake this milestone could make."""
        rendered = ExperienceContextFormatter().format(
            result(
                hit(
                    kind=ExperienceKind.FAILURE,
                    solution="这个字段必须为空",
                    root_cause="权限不足",
                    outcome_verified=True,
                ),
                guidance_count=0,
            )
        )
        assert "[Known Failure]" in rendered
        assert "Solution" not in rendered
        assert "Proven" not in rendered

    def test_a_failure_cause_is_labelled_as_a_hypothesis(self) -> None:
        rendered = ExperienceContextFormatter().format(
            result(hit(kind=ExperienceKind.FAILURE, solution=None), guidance_count=0)
        )
        assert "Hypothesized cause: 应用密码缺少 edit_posts" in rendered
        assert "Root cause:" not in rendered

    def test_an_unverified_block_keeps_its_qualified_label(self) -> None:
        rendered = ExperienceContextFormatter().format(
            result(hit(status=ExperienceStatus.DISTILLED, outcome_verified=False))
        )
        assert "[Unverified Recovery Observation]" in rendered

    def test_an_unverified_block_does_not_claim_a_root_cause(self) -> None:
        rendered = ExperienceContextFormatter().format(
            result(hit(status=ExperienceStatus.DISTILLED, outcome_verified=False))
        )
        assert "Root cause:" not in rendered

    def test_a_deprecated_block_is_labelled_deprecated(self) -> None:
        rendered = ExperienceContextFormatter().format(
            result(hit(status=ExperienceStatus.DEPRECATED))
        )
        assert "[Deprecated]" in rendered

    def test_guidance_and_warnings_both_appear_in_one_context(self) -> None:
        """Section 93's shape: what worked, and what did not, in the same block."""
        rendered = ExperienceContextFormatter().format(
            result(
                hit(experience_id="exp-a"),
                hit(kind=ExperienceKind.FAILURE, solution=None, experience_id="exp-b"),
                guidance_count=1,
            )
        )
        assert "[Verified Recovery]" in rendered
        assert "[Known Failure]" in rendered
        assert rendered.index("[Verified Recovery]") < rendered.index("[Known Failure]")

    def test_every_context_opens_by_saying_what_it_is(self) -> None:
        """The prompt-injection boundary: retrieved text is data, not instruction."""
        rendered = ExperienceContextFormatter().format(result(hit()))
        assert rendered.startswith(PREAMBLE)

    def test_an_empty_result_says_so_in_words(self) -> None:
        assert ExperienceContextFormatter().format(result()) == "No relevant experience found."


class TestBudget:
    """Section 54: do not fill the context window."""

    def test_a_long_context_is_truncated_with_a_marker(self) -> None:
        many = [hit(experience_id=f"exp-{i}") for i in range(40)]
        rendered = ExperienceContextFormatter(max_chars=600).format(
            result(*many, guidance_count=20)
        )
        assert len(rendered) <= 600
        assert rendered.endswith(TRUNCATION_MARKER)

    def test_a_short_context_is_left_alone(self) -> None:
        rendered = ExperienceContextFormatter().format(result(hit()))
        assert not rendered.endswith(TRUNCATION_MARKER)

    def test_truncation_keeps_whole_lines(self) -> None:
        many = [hit(experience_id=f"exp-{i}") for i in range(40)]
        rendered = ExperienceContextFormatter(max_chars=600).format(
            result(*many, guidance_count=20)
        )
        body = rendered[: -len(TRUNCATION_MARKER)]
        assert not body.splitlines()[-1].endswith(" ")

    def test_the_default_budget_is_a_small_fraction_of_a_context(self) -> None:
        assert 500 <= DEFAULT_MAX_CHARS <= 4000

    def test_a_budget_too_small_for_one_block_is_refused(self) -> None:
        with pytest.raises(ValueError, match="too small"):
            ExperienceContextFormatter(max_chars=50)


class TestNoRawTrace:
    """Section 53, and the reason the projection never carried these fields."""

    @pytest.mark.parametrize(
        "field",
        ["stack_trace", "raw_payload", "authorization", "cookie", "api_key", "events"],
    )
    def test_a_hit_has_no_field_for_raw_trace_material(self, field: str) -> None:
        assert field not in RetrievalHit.model_fields
