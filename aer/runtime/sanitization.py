"""Sanitisation applied before uncontrolled data reaches storage.

This is the Sanitizer of agent.md #39, now applied to every structured write path:
**Agent → Sanitizer → Storage**. Two complementary halves, always used together:

* **value-shape redaction** (:func:`redact`) -- catches credentials written into
  free text: Bearer tokens, ``key=value`` assignments, PEM blocks, SSH key bodies,
  provider key prefixes, URLs with inline passwords, header-style
  ``Authorization:`` / ``Cookie:`` lines;
* **key-based redaction** (:func:`is_secret_key`) -- catches the same credential
  structured as ``{"api_key": "abc123"}``, where the value on its own has no
  recognisable shape. A secret-keyed subtree is replaced wholesale: recursing into
  it would still be reading the secret in order to forget it.

:func:`sanitize_json` is the recursive walker that applies both halves to an
arbitrary JSON payload, with explicit markers for every transformation it makes:
a truncated string says how many characters it dropped, a subtree past the depth
limit says so. Silent truncation and silent redaction are both forbidden -- an
operator must be able to see that the Sanitizer fired.

The rules stay conservative: they match credential *shapes* and unambiguous key
names, and leave ordinary prose, file paths, hashes and identifiers alone. A false
positive silently destroys real trace data, which is worse than a missed redaction
that another rule may still catch.
"""

from __future__ import annotations

import re
import traceback
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:  # pragma: no cover - typing only, and a runtime import would be
    # circular: aer.runtime.serialization imports safe_repr from this module.
    from aer.runtime.serialization import JsonObject, JsonValue

#: Replacement written in place of a detected credential.
REDACTED = "[REDACTED]"

#: Default cap on a fallback ``repr``. Overridable per call via ``max_length``.
FALLBACK_REPR_MAX_LENGTH = 4096

#: Cap on any single string inside a sanitised JSON payload. Chosen to match the
#: adapter protocol's own string cap (round-8 brief, section 30): one limit
#: everywhere, so a payload that was legal on the way in stays legal on the way
#: into storage. Longer strings are cut with an explicit marker, never silently.
JSON_STRING_MAX_LENGTH = 4096

#: Maximum nesting depth of a sanitised JSON payload. Deeper subtrees are replaced
#: with an explicit marker. Depth, not total size, is what protects the storage
#: layer from pathological structures; the string cap bounds the leaves.
JSON_MAX_DEPTH = 8

#: Marker replacing a subtree that exceeded :data:`JSON_MAX_DEPTH`.
_DEPTH_MARKER = f"...<[truncated: deeper than {JSON_MAX_DEPTH} levels]>"

#: Cap on a verifier-supplied ``message``. A verifier is free-form code and may
#: hand back an entire HTML page or server response as its explanation; the column
#: is an audit field, not a blob store.
MESSAGE_MAX_LENGTH = 4096

#: Cap on a stack trace that came from *outside* AER (Milestone 8).
#:
#: The in-process path does not cap tracebacks: AER produced them, and a traceback is
#: bounded by the interpreter's own frames. An adapter-received trace is different --
#: it is a string a vendor sent us, possibly a few megabytes of build log pasted into
#: a ``stack`` field -- so it is capped with an explicit marker rather than trusted.
EXTERNAL_STACK_TRACE_MAX_LENGTH = 16_384

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
    # Authorization: <anything> / Cookie: <anything> -- header-style appearances.
    # ``Set-Cookie`` lands here too: the boundary at the start of ``cookie`` sits
    # inside the hyphenated word, which ``\b`` treats as one word character run...
    # so match the header names with an optional ``set-`` prefix instead.
    (re.compile(r"(?i)\bauthorization\b['\"]?\s*[:=]\s*\S+"), f"authorization={REDACTED}"),
    (re.compile(r"(?i)\b(?:set-)?cookie\b['\"]?\s*[:=]\s*\S+"), f"cookie={REDACTED}"),
    # key = value / "key": "value" credential assignments. The token family names
    # each compound spelling explicitly: ``refresh_token`` is one word to a regex
    # with ``\b``, so ``token`` alone would never match inside it.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?(?:key|token)|refresh[_-]?token|"
            r"id[_-]?token|session[_-]?token|client[_-]?secret|secret|token|"
            r"password|passwd|pwd)\b"
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


#: Substrings that make a key name unambiguous about holding a credential.
#:
#: Checked as substrings because these words have no innocent uses: nothing legitimate
#: is called ``authorization_header`` except an authorization header.
_CONTAINS_SECRET_KEYS: tuple[str, ...] = (
    "authorization",
    "password",
    "passwd",
    "credentials",
    "apikey",
    "secretkey",
    "privatekey",
    "cookie",
)

#: Short or ambiguous key names that must match exactly.
#:
#: ``auth``, ``token`` and ``secret`` live here rather than above because they are
#: plausible parts of unrelated names -- matching them as substrings would redact an
#: ``author`` or count keys called ``token_count``.
_EXACT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "auth",
        "apikey",
        "accesstoken",
        "accesskey",
        "bearer",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "secret",
        "signature",
        "signingkey",
        "token",
        "idtoken",
        "refreshtoken",
        "sessionkey",
    }
)

#: Suffixes that make a key name a credential however it is prefixed.
_SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "accesstoken",
    "apikey",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "signingkey",
    "token",
)


def is_secret_key(name: str) -> bool:
    """Whether a key's *value* is a credential by virtue of what the key is called.

    Value-shape redaction (:func:`redact`) catches ``api_key=abc123`` written into a
    string. It cannot catch the same thing structured as ``{"api_key": "abc123"}``,
    because ``abc123`` on its own has no recognizable shape. This is that missing half,
    and the two are always used together.

    Deliberately conservative in the ambiguous cases: ``author`` is not a credential,
    and neither is a key called ``token_count``. A false positive silently destroys
    real data, which is worse than a missed redaction that the shape rules may still
    catch.
    """
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    if not normalized:
        return False
    if normalized in _EXACT_SECRET_KEYS:
        return True
    if any(hint in normalized for hint in _CONTAINS_SECRET_KEYS):
        return True
    return normalized.endswith(_SECRET_KEY_SUFFIXES)


@overload
def sanitize_json(value: JsonObject) -> JsonObject: ...
@overload
def sanitize_json(value: JsonValue) -> JsonValue: ...


def sanitize_json(value: JsonValue) -> JsonValue:
    """Recursively sanitise one JSON payload: redact, cap, and mark every cut.

    The single entry point of the Sanitizer (agent.md #39). Every string is passed
    through :func:`redact` and capped at :data:`JSON_STRING_MAX_LENGTH` with the
    explicit truncation marker; every dict member whose key :func:`is_secret_key`
    recognises is replaced wholesale by :data:`REDACTED` -- the subtree is *not*
    recursed into, because reading a credential in order to forget it is still
    reading it; and anything past :data:`JSON_MAX_DEPTH` levels of nesting becomes
    the depth marker. Numbers and booleans pass through untouched.

    Idempotent on its own output, which matters because some write paths sanitise
    a body before handing it to a seam that sanitises again (the adapter ingest
    path and ``RunContext.emit`` are both sanitisers): ``REDACTED`` matches no
    secret pattern, the truncation marker fits the length cap, and the depth
    marker is a plain string at the level where it appears.

    Callers normalise arbitrary objects into the JSON contract *before* calling
    this; it deliberately accepts only ``JsonValue`` so this module never imports
    the serialisation module that imports it.
    """
    return _sanitize_node(value, depth=0)


def _sanitize_node(value: JsonValue, *, depth: int) -> JsonValue:
    if isinstance(value, str):
        capped = value
        if len(capped) > JSON_STRING_MAX_LENGTH:
            capped = truncate(capped, JSON_STRING_MAX_LENGTH)
        return redact(capped)
    if isinstance(value, list):
        if depth >= JSON_MAX_DEPTH:
            return _DEPTH_MARKER
        return [_sanitize_node(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if depth >= JSON_MAX_DEPTH:
            return _DEPTH_MARKER
        cleaned: dict[str, JsonValue] = {}
        for key, item in value.items():
            if is_secret_key(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = _sanitize_node(item, depth=depth + 1)
        return cleaned
    # bool, int, float, None: nothing a credential can hide in.
    return value


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
