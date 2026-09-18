"""The user text -> engine expression boundary.

Every hostile input in this file was actually fed to neug 0.2.0 during the probe and
raised, which is why the assertions are about *structure* -- no caller-derived text
may reach the engine unquoted -- rather than about any particular string coming out.
The engine's query language is an implementation detail; that an operator in it can
never be written by caller text is the contract.
"""

from __future__ import annotations

import pytest

from aer.knowledge.query import MAX_TERMS, fts_expression, query_terms

#: Inputs that made the engine raise when passed through unchanged.
ENGINE_KILLERS = (
    "wp-json 404",
    "zzz-nothing",
    "a:b",
    "AND",
    "NOT x",
    "a (b)",
    "site-health*",
    "x NEAR y",
    'a" OR "b',
    "^start",
    "end$",
)

#: Inputs with no searchable word in them at all. Kept apart from the list above
#: because the correct output is different in kind: nothing to search for, rather
#: than a neutralised query.
NOTHING_TO_SEARCH = ("!!!", "   ", "。！？", "...", "()[]")


class TestTokenising:
    """What counts as a searchable word."""

    def test_ascii_words_are_terms(self) -> None:
        assert query_terms("WordPress REST API") == ("WordPress", "REST", "API")

    def test_a_hyphenated_word_becomes_two_terms(self) -> None:
        """``wp-json`` is indexed as two tokens, so it must be searched as two.

        Keeping it whole would produce a term nothing can match, which reads as
        "no such experience" when the knowledge exists.
        """
        assert query_terms("wp-json") == ("wp", "json")

    def test_chinese_is_kept_as_a_run(self) -> None:
        """CJK has no spaces; the engine's tokenizer splits it, not this layer."""
        assert query_terms("多维表格字段类型") == ("多维表格字段类型",)
        assert query_terms("分类页 标题") == ("分类页", "标题")

    def test_mixed_scripts_keep_their_order(self) -> None:
        assert query_terms("WordPress 多维表格 API 询盘") == (
            "WordPress",
            "多维表格",
            "API",
            "询盘",
        )

    def test_punctuation_is_dropped_rather_than_quoted(self) -> None:
        assert query_terms("!!!") == ()
        assert query_terms("...") == ()
        assert query_terms("a (b)") == ("a", "b")

    def test_digits_and_underscores_are_terms(self) -> None:
        assert query_terms("403 _private v2") == ("403", "_private", "v2")

    def test_whitespace_only_has_no_terms(self) -> None:
        assert query_terms("   \t\n ") == ()

    def test_terms_are_capped(self) -> None:
        """A pasted document is not a query; the cap bounds what the engine parses."""
        expression = fts_expression(" ".join(f"word{i}" for i in range(MAX_TERMS + 50)))
        assert expression.count(" OR ") == MAX_TERMS - 1
        assert "word0" in expression
        assert f"word{MAX_TERMS + 49}" not in expression


class TestExpression:
    """The expression handed to the engine."""

    def test_terms_are_joined_with_or(self) -> None:
        assert fts_expression("WordPress REST") == '"WordPress" OR "REST"'

    def test_a_chinese_query_stays_one_phrase(self) -> None:
        assert fts_expression("多维表格") == '"多维表格"'

    @pytest.mark.parametrize("hostile", ENGINE_KILLERS)
    def test_no_caller_text_escapes_quoting(self, hostile: str) -> None:
        """The property that makes the boundary safe, over the real hostile inputs.

        Every caller-derived word has to sit inside a double-quoted phrase, and the
        only text the engine sees unquoted is the ``OR`` this module emits itself.
        Anything else -- an operator, a bracket, a wildcard -- would be a piece of
        the FTS5 query language the caller got to write.
        """
        expression = fts_expression(hostile)
        outside = _outside_quotes(expression)
        assert outside.replace("OR", "").strip() == "", expression
        assert not any(char in outside for char in "-!():^*"), expression

    @pytest.mark.parametrize("hostile", ENGINE_KILLERS)
    def test_every_piece_is_a_complete_quoted_phrase(self, hostile: str) -> None:
        for piece in fts_expression(hostile).split(" OR "):
            assert piece.startswith('"') and piece.endswith('"'), piece
            assert len(piece) > 2, piece
            assert '"' not in piece[1:-1], piece

    @pytest.mark.parametrize("hostile", ENGINE_KILLERS)
    def test_hostile_input_always_produces_terms(self, hostile: str) -> None:
        """Sanity check on the parameterisation itself: these all contain words."""
        assert fts_expression(hostile) != ""

    @pytest.mark.parametrize("empty", NOTHING_TO_SEARCH)
    def test_nothing_searchable_produces_nothing(self, empty: str) -> None:
        """An empty expression is a *result*, and the caller must handle it.

        Passing it on would be a syntax error, so the index treats "" as "no
        results" rather than as a query.
        """
        assert fts_expression(empty) == ""

    def test_the_token_pattern_cannot_produce_a_quote(self) -> None:
        """Which is why the doubling escape is unreachable *today*.

        The escape still exists in the expression builder: the guarantee that no
        caller text can break the syntax belongs to that function, and a future
        token pattern that admitted a quote must not silently start emitting broken
        FTS5.
        """
        for text in ('say"hi', "a''b", '"""', "“smart”"):
            assert all('"' not in term for term in query_terms(text))


def _outside_quotes(text: str) -> str:
    """Everything in ``text`` that is not inside a double-quoted run."""
    return "".join(text.split('"')[::2])
