"""Sanitising and fingerprinting: what may be stored, and what a key means.

Small unit tests, because these functions are the boundary between an agent's context
and a durable table. Everything else in the usage store is structured; this is the one
place where uncontrolled text could get in, so it gets tested on its own rather than
only through the retrieval path.
"""

from __future__ import annotations

from aer.usage.fingerprints import (
    QUERY_FINGERPRINT_LENGTH,
    RETRIEVAL_QUERY_MAX_LENGTH,
    context_fingerprint,
    normalize_text,
    query_fingerprint,
    sanitize_query,
)


class TestNormalizeText:
    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_text("  a\n\n b\tc  ") == "a b c"

    def test_an_empty_string_stays_empty(self) -> None:
        assert normalize_text("   \n ") == ""


class TestSanitizeQuery:
    def test_ordinary_text_is_left_alone(self) -> None:
        assert sanitize_query("WordPress REST API 403 on page update") == (
            "WordPress REST API 403 on page update"
        )

    def test_a_bearer_token_is_redacted(self) -> None:
        cleaned = sanitize_query("call with Authorization: Bearer abcdef123456")

        assert "abcdef123456" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_a_long_query_is_capped_inside_the_limit(self) -> None:
        cleaned = sanitize_query("word " * 1000)

        assert len(cleaned) <= RETRIEVAL_QUERY_MAX_LENGTH

    def test_a_custom_cap_is_honoured(self) -> None:
        assert len(sanitize_query("abcdefghij", max_length=5)) <= 5


class TestQueryFingerprint:
    def test_it_is_a_full_digest(self) -> None:
        assert len(query_fingerprint("anything")) == QUERY_FINGERPRINT_LENGTH

    def test_case_and_spacing_do_not_split_a_question(self) -> None:
        assert query_fingerprint("REST API 403") == query_fingerprint("  rest   api 403 ")

    def test_different_questions_differ(self) -> None:
        assert query_fingerprint("REST API 403") != query_fingerprint("REST API 404")

    def test_it_is_stable_across_calls(self) -> None:
        assert query_fingerprint("the same question") == query_fingerprint("the same question")


class TestContextFingerprint:
    def test_identical_contexts_share_a_digest(self) -> None:
        assert context_fingerprint("rendered context") == context_fingerprint("rendered context")

    def test_a_different_rendering_differs(self) -> None:
        assert context_fingerprint("rendered context") != context_fingerprint("Rendered context")

    def test_the_digest_reveals_nothing_of_its_input(self) -> None:
        """The whole reason a fingerprint is stored instead of the text."""
        digest = context_fingerprint("api_key=sk-ABCDEFGHIJKLMNOPQRSTUV")

        assert "sk-" not in digest
        assert len(digest) == 64
