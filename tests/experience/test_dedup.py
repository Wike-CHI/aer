"""Deterministic duplicate detection (Milestone 5, sections 39-40).

The fingerprint decides whether a recurring problem joins existing knowledge or
becomes a new record. Both errors are expensive -- a duplicate row wastes retrieval
slots, a wrong merge attaches evidence to an unrelated claim -- and the failure modes
are asymmetric, so normalisation is deliberately shallow.
"""

from __future__ import annotations

from aer import ExperienceKind, dedup_key_for, normalise_text


def key(**overrides: object) -> str:
    payload: dict[str, object] = {
        "kind": ExperienceKind.RECOVERY,
        "domain": "wordpress",
        "title": "REST API 403",
        "problem": "page update returns 403",
    }
    payload.update(overrides)
    return dedup_key_for(**payload)  # type: ignore[arg-type]


class TestNormalisation:
    def test_case_and_whitespace_are_ignored(self) -> None:
        assert normalise_text("WordPress   REST\nAPI") == "wordpress rest api"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert normalise_text("  spaced  ") == "spaced"

    def test_full_width_characters_fold_onto_ascii(self) -> None:
        """Text pasted out of a browser or a CJK IME is the same problem."""
        assert normalise_text("ＷｏｒｄＰｒｅｓｓ ４０３") == "wordpress 403"  # noqa: RUF001  # noqa: RUF001

    def test_tabs_and_newlines_become_one_space(self) -> None:
        assert normalise_text("a\t\tb\n\nc") == "a b c"

    def test_an_empty_string_stays_empty(self) -> None:
        assert normalise_text("   ") == ""


class TestFingerprint:
    def test_the_same_claim_gets_the_same_key(self) -> None:
        assert key() == key()

    def test_formatting_differences_do_not_change_the_key(self) -> None:
        assert key(title="  REST   API 403 ") == key(title="rest api 403")
        assert key(problem="PAGE UPDATE RETURNS 403") == key()

    def test_a_different_kind_is_a_different_claim(self) -> None:
        """ "this failed" and "this worked" are not the same statement, even about
        the same words; merging them would delete the distinction the store exists
        for."""
        assert key(kind=ExperienceKind.FAILURE) != key(kind=ExperienceKind.RECOVERY)
        assert key(kind=ExperienceKind.SUCCESS) != key(kind=ExperienceKind.RECOVERY)

    def test_a_different_domain_is_a_different_claim(self) -> None:
        assert key(domain="shopify") != key()

    def test_a_different_title_is_a_different_claim(self) -> None:
        assert key(title="REST API 401") != key()

    def test_a_different_problem_is_a_different_claim(self) -> None:
        assert key(problem="page update returns 500") != key()

    def test_the_key_is_a_stable_hex_digest(self) -> None:
        value = key()

        assert len(value) == 64
        assert all(character in "0123456789abcdef" for character in value)

    def test_punctuation_is_deliberately_not_stripped(self) -> None:
        """D-035: stripping punctuation can merge genuinely different problems, and a
        wrong merge is worse than a missed one. Ambiguity is left to retrieval, where
        a human-visible ranking can carry it."""
        assert key(title="REST API 403!") != key(title="REST API 403")

    def test_near_synonyms_are_not_merged(self) -> None:
        assert key(title="REST API 403") != key(title="REST API forbidden")

    def test_field_boundaries_cannot_be_confused(self) -> None:
        """Different splits of the same characters are different claims."""
        assert key(title="a b", problem="c") != key(title="a", problem="b c")
