"""Turning uncontrolled text into something safe to store, and into a stable key.

Three jobs, all of them about *not* keeping things:

**The stored query is sanitised.** A retrieval query is derived from an agent's
context, and that context may contain a prompt, a tool response or a credential. Only
the retrieval question itself is stored, whitespace-collapsed, redacted and capped
(round-7 brief, section 6). The full prompt never reaches the usage tables at all
(sections 19 and 61).

**The stored context is a fingerprint.** Injection records *that* a context was
rendered and which version rendered it -- never the rendered text. A hash is enough
to prove two captures are the same and to detect that a re-render differs, and it
cannot leak the payload it describes (section 19).

**The fingerprint is derived from the sanitised form.** If it were derived from the
raw text, a token pasted into a question would silently change the grouping key and
two identical problems would look like two different ones. Deriving it after
redaction also means the key is safe to log.

Fingerprints are full SHA-256 hex digests rather than truncated prefixes. The
collision probability of a short prefix is negligible for this data, but "negligible"
is not "impossible", and a collision here would merge two different questions into one
statistic.
"""

from __future__ import annotations

import hashlib

from aer.runtime.sanitization import redact, truncate

__all__ = [
    "CONTEXT_FINGERPRINT_LENGTH",
    "QUERY_FINGERPRINT_LENGTH",
    "RETRIEVAL_QUERY_MAX_LENGTH",
    "context_fingerprint",
    "normalize_text",
    "query_fingerprint",
    "sanitize_query",
]

#: Cap on the stored retrieval query. Generous for a question, far below the size of
#: the prompt it was derived from. Truncation is counted inside the limit, so a
#: column can be sized against this constant directly.
RETRIEVAL_QUERY_MAX_LENGTH = 512

#: Length of the hex digests below. Published so tests and operators can reason about
#: the stored value without importing hashlib.
QUERY_FINGERPRINT_LENGTH = 64
CONTEXT_FINGERPRINT_LENGTH = 64


def normalize_text(value: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    Newlines matter here: a query pasted from a trace arrives with line breaks, and a
    stored multi-line "query" would break the one-line rendering of every report that
    shows it. The search itself is unaffected -- the retriever normalises through
    :mod:`aer.knowledge.query` -- so this only tames the record.
    """
    return " ".join(value.split())


def sanitize_query(query: str, *, max_length: int = RETRIEVAL_QUERY_MAX_LENGTH) -> str:
    """Return the form of ``query`` that may be stored.

    Redaction runs before truncation so a credential near the end of a long query is
    never the part that survives.
    """
    return truncate(redact(normalize_text(query)), max_length)


def query_fingerprint(sanitized_query: str) -> str:
    """Stable key for "the same question", derived from an already-sanitised query.

    Case-folded, because "WordPress REST API 403" and "wordpress rest api 403" are the
    same question being asked twice, and grouping them is the point of the key.
    """
    return _digest(normalize_text(sanitized_query).casefold())


def context_fingerprint(context: str) -> str:
    """Digest of a rendered context, for recording an injection without its text.

    Case is **not** folded: this identifies a byte-for-byte rendering, and a
    formatter change that altered capitalisation is exactly the kind of difference a
    caller comparing two captures wants to see.
    """
    return _digest(context)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
