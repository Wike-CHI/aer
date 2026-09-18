"""Turning a caller's words into a full-text expression the engine will accept.

This module exists because of a fact that is easy to miss and expensive to learn
in production: NeuG's ``bm25()`` does not take text to search for. It takes a
**query in the index's own language**, which -- as the probe against neug 0.2.0
showed -- is SQLite FTS5 syntax. Passing caller text through unchanged means:

===============================  ==========================================
caller types                     what the engine does
===============================  ==========================================
``wp-json 404``                  ``-`` parses as NOT -> "no such column: json"
``!!!``                          ``fts5: syntax error near "!"``
``say "hi"``                     unbalanced phrase quote
``分类页``                        matches nothing, returns ``[]`` (fine)
===============================  ==========================================

So a user looking for "wp-json 404" gets an exception where they expected "no
relevant experience" -- and the exception comes from a *syntax* problem, which
tells them nothing about the corpus. Retrieval must never fail because of the
shape of a word.

The fix is to stop treating the query as syntax at all: extract the words, and
hand each one back to the engine as a **quoted literal**, where ``-``, ``!``,
``AND`` and friends are ordinary characters. Quotes inside a token are doubled,
which is FTS5's own escape.

Two judgement calls worth stating:

**Terms are OR-ed, not AND-ed.** The engine's implicit join is AND, which makes a
six-word query fail whenever the stored problem statement happens not to repeat
one of the words. Recall is cheap here because BM25 already orders the result by
how much of the query each document matches; a term that is simply absent
contributes nothing instead of excluding everything.

**Tokens are capped.** A pasted document is not a search query. Keeping the first
:data:`MAX_TERMS` terms bounds the expression the engine has to parse and keeps one
caller from turning retrieval into a parser benchmark.
"""

from __future__ import annotations

import re

__all__ = ["MAX_TERMS", "TOKEN_PATTERN", "fts_expression", "query_terms"]

#: How many terms of a query are used. Beyond this the expression costs more to
#: parse than the extra terms can add to the ranking.
MAX_TERMS = 32

#: One term: either an ASCII word (letters, digits, underscore) or a run of
#: non-ASCII word characters -- which is what a Chinese or other non-Latin word
#: looks like once punctuation has been excluded.
#:
#: Written as an alternation rather than ``\w+`` so that ``wp-json`` becomes two
#: terms (matching how the tokenizer indexed it) instead of one term that could
#: never match, and so that punctuation is dropped rather than quoted into
#: meaningless terms.
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z_]+|[^\W\d_A-Za-z]+", re.UNICODE)


def query_terms(text: str) -> tuple[str, ...]:
    """The searchable terms in ``text``, in order, capped at :data:`MAX_TERMS`.

    Returns an empty tuple for input that contains no words at all -- punctuation,
    whitespace, emoji. Callers must treat that as "nothing to search for" rather
    than passing it on, because an empty expression is itself a syntax error.
    """
    terms = [match.group(0) for match in TOKEN_PATTERN.finditer(text)]
    return tuple(terms[:MAX_TERMS])


def fts_expression(text: str) -> str:
    """A safe full-text expression for ``text``, or ``""`` when there is nothing.

    An empty return value is a real answer, not a failure: it means the caller sent
    no searchable words, and the correct response is an empty result set rather
    than a query whose syntax the engine will reject.
    """
    terms = query_terms(text)
    if not terms:
        return ""
    return " OR ".join(_quote(term) for term in terms)


def _quote(term: str) -> str:
    """Quote ``term`` as an FTS5 phrase literal.

    Only the double quote needs escaping (by doubling it, FTS5's convention);
    everything else inside a quoted string is inert. ``query_terms`` cannot
    actually produce a quote today because the token pattern excludes punctuation,
    which is exactly why the escape is written here rather than left out: it is
    the guarantee, not the current input, that this function is responsible for.
    """
    return '"' + term.replace('"', '""') + '"'
