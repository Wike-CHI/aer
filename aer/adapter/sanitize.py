"""Treating everything an adapter sends as untrusted external data (sections 25-27).

Adapters are the one place where text AER did not produce enters the store. A tool
result may contain an API key, a build log may contain a database URL, a session may
contain a person's name, and a vendor may faithfully forward an entire conversation
into a field AER never meant to hold one. The in-process SDK trusts its caller because
the caller *is* the agent AER is observing; the adapter path cannot, so it sanitizes.

Four things happen here, and each one is a different failure being prevented:

**Credential shapes are redacted** -- by value (:func:`~aer.runtime.sanitization.redact`)
and by key name (:func:`~aer.runtime.sanitization.is_secret_key`). Both halves are
needed: ``api_key=abc`` appears in strings, ``{"api_key": "abc"}`` does not.

**Private reasoning is dropped, not redacted** (section 6). A chain of thought, a
scratchpad, a hidden-reasoning field: AER does not store these even in redacted form,
because the point is not that they might contain a secret, it is that they are not
AER's business and not the agent's public explanation. What *is* recorded is a
``decision_summary`` -- an auditable statement of why an action was taken. The names of
whatever was dropped are recorded, so the removal is visible rather than silent.

**Sizes are capped with an explicit marker** (sections 27 and 45). Every string is
capped; the whole body has a cap; and anything that has to be cut says how much was
lost. Silent truncation is the one outcome that is never acceptable, because a reader
cannot tell a complete record from a shortened one.

**Prompt injection is not "handled", it is structurally out of reach** (section 26).
There is nothing to sanitize here: text inside a tool result or a web page is stored as
an *observation*, and the protocol has no field through which an external string could
become an AER directive. The rule is enforced by the absence of such a field, which is
why it is stated here rather than implemented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aer.runtime.sanitization import REDACTED, is_secret_key, redact, truncate
from aer.runtime.serialization import JsonObject, JsonValue, to_json_value

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_DECISION_SUMMARY_CHARS",
    "MAX_STRING_CHARS",
    "PRIVATE_REASONING_KEYS",
    "SanitizedBody",
    "sanitize_external_body",
    "sanitize_external_value",
]

#: Longest single string kept inside an external body.
MAX_STRING_CHARS = 4096
#: Longest ``decision_summary`` kept. Shorter than a generic string because a summary
#: that needs four thousand characters is not a summary (section 27).
MAX_DECISION_SUMMARY_CHARS = 2048
#: Largest serialized body accepted before it is replaced by a truncation marker.
MAX_BODY_CHARS = 16_384
#: How deep the walker descends before replacing the rest with a marker. A vendor
#: payload nested a thousand levels deep is a denial-of-service attempt, not data.
MAX_DEPTH = 8

#: Key names whose values are never stored, matched exactly (section 6).
#:
#: Exact rather than substring matching so that a legitimate key such as
#: ``decision_summary`` or ``reasoning_summary`` is not silently removed along with the
#: private ones. The list is the vocabulary vendors actually use for hidden reasoning;
#: anything outside it is still subject to size caps and credential redaction.
PRIVATE_REASONING_KEYS: frozenset[str] = frozenset(
    {
        "chain_of_thought",
        "chainofthought",
        "cot",
        "hidden_reasoning",
        "hiddenreasoning",
        "internal_monologue",
        "reasoning",
        "scratchpad",
        "scratch_pad",
        "thinking",
        "thoughts",
    }
)

#: Marker written in place of a body that was too large to keep whole.
TRUNCATED_MARKER_KEY = "__aer_truncated__"


@dataclass(frozen=True, slots=True)
class SanitizedBody:
    """A sanitized event body, plus what had to be done to it.

    The notes travel back to the caller so they can be recorded on the event's
    metadata. A payload that lost a key, or had one redacted, is still a faithful
    record of the event -- as long as the loss is written down next to it.
    """

    body: JsonObject
    redacted_keys: tuple[str, ...] = ()
    dropped_keys: tuple[str, ...] = ()
    truncated_strings: int = 0

    @property
    def was_modified(self) -> bool:
        """Whether anything was redacted, dropped or cut."""
        return bool(self.redacted_keys or self.dropped_keys or self.truncated_strings)

    def notes(self) -> JsonObject:
        """The modification record, ready to merge into event metadata."""
        return {
            "redacted_keys": list(self.redacted_keys),
            "dropped_private_keys": list(self.dropped_keys),
            "truncated_strings": self.truncated_strings,
        }


class _Walk:
    """Mutable accumulator threaded through one traversal.

    A small class rather than a bag of return values because the traversal has three
    things to report and returning a tuple at every level would make the recursion
    unreadable.
    """

    def __init__(self) -> None:
        self.redacted_keys: list[str] = []
        self.dropped_keys: list[str] = []
        self.truncated_strings = 0

    def string(self, value: str, *, limit: int) -> str:
        """Redact, then cap -- in that order, so a secret near the end never survives.

        The order matters: truncating first could cut a credential in half and leave a
        prefix of it behind, which is exactly the part that is still useful to an
        attacker.
        """
        cleaned = redact(value)
        if len(cleaned) > limit:
            self.truncated_strings += 1
            return truncate(cleaned, limit)
        return cleaned


def sanitize_external_value(
    value: JsonValue,
    *,
    walk: _Walk | None = None,
    depth: int = 0,
    string_limit: int = MAX_STRING_CHARS,
) -> JsonValue:
    """Recursively sanitize a JSON value that came from outside AER.

    Args:
        value: The value to clean. Already JSON-shaped: callers normalise first with
            :func:`~aer.runtime.serialization.to_json_value`, so this function never has
            to guess what an arbitrary Python object meant.
        walk: The accumulator, supplied by the recursion.
        depth: Current nesting depth.
        string_limit: Cap applied to strings at this level.

    Returns:
        A value of the same shape, with credential values replaced, private-reasoning
        keys removed, and oversized strings cut with an explicit marker.
    """
    walk = walk if walk is not None else _Walk()

    if depth > MAX_DEPTH:
        walk.dropped_keys.append(f"<depth>{MAX_DEPTH}")
        return {"__aer_omitted__": "nesting deeper than the protocol allows"}

    if isinstance(value, str):
        return walk.string(value, limit=string_limit)

    if isinstance(value, list):
        return [
            sanitize_external_value(item, walk=walk, depth=depth + 1, string_limit=string_limit)
            for item in value
        ]

    if isinstance(value, dict):
        cleaned: dict[str, JsonValue] = {}
        for key, item in value.items():
            if key.casefold() in PRIVATE_REASONING_KEYS:
                walk.dropped_keys.append(str(key))
                continue
            if is_secret_key(key):
                walk.redacted_keys.append(str(key))
                cleaned[str(key)] = REDACTED
                continue
            limit = MAX_DECISION_SUMMARY_CHARS if key == "decision_summary" else string_limit
            cleaned[str(key)] = sanitize_external_value(
                item, walk=walk, depth=depth + 1, string_limit=limit
            )
        return cleaned

    return value


def sanitize_external_body(
    body: Any,
    *,
    max_chars: int = MAX_BODY_CHARS,
) -> SanitizedBody:
    """Sanitize one event body and enforce its overall size cap.

    The body is normalised for JSON first, so a caller may pass any Python structure.
    If the sanitized result still exceeds ``max_chars`` -- many strings, each within its
    own cap -- the whole body is replaced by a marker naming the size and the keys that
    were present. Replacing rather than rejecting keeps the *fact* that the event
    happened, which is what the trace is for; the content it could not hold is stated
    instead of pretended away.
    """
    walk = _Walk()
    normalized = to_json_value(body)
    cleaned = sanitize_external_value(normalized, walk=walk)

    if not isinstance(cleaned, dict):
        cleaned = {"value": cleaned}

    measured = _serialized_length(cleaned)
    if measured > max_chars:
        walk.truncated_strings += 1
        present: list[JsonValue] = [str(key) for key in sorted(cleaned)]
        cleaned = {
            TRUNCATED_MARKER_KEY: True,
            "reason": f"body of {measured} characters exceeds the {max_chars} character cap",
            "keys": present,
        }

    return SanitizedBody(
        body=cleaned,
        redacted_keys=tuple(walk.redacted_keys),
        dropped_keys=tuple(walk.dropped_keys),
        truncated_strings=walk.truncated_strings,
    )


def _serialized_length(value: JsonObject) -> int:
    """Character count of the body once encoded, matching what storage will hold."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
