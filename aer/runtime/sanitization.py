"""Minimal sanitisation applied before uncontrolled text reaches storage.

This is deliberately **not** the full Sanitizer framework of agent.md #39 -- that
is a later milestone. It covers the two places where uncontrolled, unmodelled text
can enter the database in this milestone:

* the ``repr`` of an object that had to be degraded into a fallback marker
  (round-3 brief, section 24);
* exception stack traces (round-3 brief, section 13).

Structured payloads supplied by the caller are stored as-is. Redacting them
wholesale would corrupt legitimate trace data, and a complete pass belongs to the
Sanitizer milestone that also owns retention rules.

The rules below are intentionally conservative: they match credential *shapes*
(Bearer tokens, ``key=value`` assignments with a token-like value, PEM blocks,
SSH key bodies, provider key prefixes, URLs with inline passwords) and leave
ordinary prose, file paths, hashes and identifiers alone.
"""

from __future__ import annotations

import re
import traceback

#: Replacement written in place of a detected credential.
REDACTED = "[REDACTED]"

#: Default cap on a fallback ``repr``. Overridable per call via ``max_length``.
FALLBACK_REPR_MAX_LENGTH = 4096

#: Cap on a verifier-supplied ``message``. A verifier is free-form code and may
#: hand back an entire HTML page or server response as its explanation; the column
#: is an audit field, not a blob store.
MESSAGE_MAX_LENGTH = 4096

#: A token-like unquoted value, but not the prefix of a longer expression.
#: The negative lookahead stops ``password = os.environ["X"]`` (a source line in a
#: stack trace) from being treated as a credential assignment.
_UNQUOTED_VALUE = r"[A-Za-z0-9._~+/=-]{4,}(?![\w\[(])"
_QUOTED_VALUE = r"['\"][^'\"]{4,}['\"]"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private key blocks -- checked first so their body is not partially
    # rewritten by the looser rules below.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        REDACTED,
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}"), f"Bearer {REDACTED}"),
    # Authorization: <anything>
    (re.compile(r"(?i)\bauthorization\b['\"]?\s*[:=]\s*\S+"), f"authorization={REDACTED}"),
    # key = value / "key": "value" credential assignments
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|pwd)\b"
            r"['\"]?\s*[:=]\s*"
            r"(?:" + _QUOTED_VALUE + r"|" + _UNQUOTED_VALUE + r")"
        ),
        rf"\1={REDACTED}",
    ),
    # Provider-specific key prefixes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{4,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    # OpenSSH key bodies
    (
        re.compile(r"(?i)\b(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp\d+)\s+[A-Za-z0-9+/=]{20,}"),
        rf"\1 {REDACTED}",
    ),
    # scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@/\s]+(@)"), rf"\1{REDACTED}\2"),
)


def redact(text: str) -> str:
    """Replace recognised credential shapes in ``text`` with ``[REDACTED]``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, max_length: int) -> str:
    """Cap ``text`` at exactly ``max_length`` characters.

    The elision marker is counted *inside* the limit, so the result never exceeds
    ``max_length``: callers can size a database column against the constant
    directly without adding slack for the annotation.
    """
    if max_length < 0:
        raise ValueError("max_length must be >= 0")
    if len(text) <= max_length:
        return text

    dropped = len(text) - max_length
    marker = f"...<truncated {dropped} chars>"
    if len(marker) >= max_length:
        return marker[:max_length]
    return text[: max_length - len(marker)] + marker


def safe_repr(value: object, *, max_length: int = FALLBACK_REPR_MAX_LENGTH) -> str:
    """Return a redacted, length-capped ``repr`` of ``value``.

    A user-defined ``__repr__`` may raise. That must not cost us the trace, so the
    failure is turned into a diagnostic string rather than propagated -- this is
    not a silent swallow, because the failure becomes part of the recorded trace.
    """
    try:
        raw = repr(value)
    except Exception as exc:
        raw = f"<repr() failed: {type(exc).__name__}: {exc}>"
    return truncate(redact(raw), max_length)


def format_exception(exc: BaseException) -> str:
    """Render an exception (and its chain) as redacted plain text.

    Plain text on purpose: the round-3 brief forbids pickling exception objects
    into the database, and a traceback string is what a human or a model actually
    needs to diagnose the failure.
    """
    return redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
