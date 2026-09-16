"""Canonical value types and conversion helpers for the AER runtime layer.

Two responsibilities live here and nowhere else:

1. **JSON value types** - ``JsonScalar`` / ``JsonValue`` / ``JsonObject`` give the
   runtime a precise, recursive JSON contract. Core domain models use these
   instead of ``Any`` or bare ``dict`` (agent.md #49).

2. **Normalisation** - :func:`to_json_value` converts an arbitrary Python object
   produced by an agent into a JSON-safe value. Losing trace data is worse than
   storing an imperfect representation of it (TASKS.md #6, Task 3.5), so an
   unsupported object is *degraded into an explicit marker* rather than raising
   or being silently flattened into a plain string.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel

from aer.runtime.sanitization import safe_repr

# PEP 695 lazy type aliases: recursive, evaluated on lookup, understood by
# Pydantic and mypy. `JsonValue` accepts any JSON value, `JsonObject` restricts
# to a JSON object (used for `metadata`).
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

#: Key marking a payload that lost fidelity during normalisation. Consumers can
#: query for it to find traces where serialisation degraded.
FALLBACK_MARKER_KEY = "__aer_fallback__"


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Every timestamp handled by AER is timezone-aware; this is the single source
    of "now" inside the runtime.
    """
    return datetime.now(UTC)


def new_id() -> str:
    """Return a new random identifier (UUID4 as a string)."""
    return str(uuid4())


def fallback_marker(value: object, *, max_length: int | None = None) -> JsonObject:
    """Describe an object AER cannot represent as JSON.

    The marker is deliberately loud. Silently storing ``str(obj)`` made two very
    different situations indistinguishable in the trace: "this was genuinely a
    string" and "AER gave up on this object". With an explicit marker, a later
    pass can find every degraded payload, and a consumer that expected structure
    can tell it is looking at a lossy placeholder.

    ``repr`` is redacted and length-capped because it may contain credentials or
    megabytes of content (round-3 brief, section 24).
    """
    value_type = type(value)
    marker: JsonObject = {
        FALLBACK_MARKER_KEY: True,
        "python_type": f"{value_type.__module__}.{value_type.__qualname__}",
        "repr": safe_repr(value) if max_length is None else safe_repr(value, max_length=max_length),
    }
    return marker


def to_json_value(value: object) -> JsonValue:
    """Recursively convert ``value`` into a JSON-safe value.

    Conversions applied, in order of precedence:

    * ``Enum``            -> its ``value``, converted recursively
    * ``BaseModel``       -> ``model_dump(mode="json")``, converted recursively
    * ``datetime``/``date``/``time`` -> ISO 8601 string
    * ``bool``/``int``/``float``/``str``/``None`` -> unchanged
    * ``Mapping``         -> ``dict[str, JsonValue]`` (keys stringified)
    * ``list``/``tuple``/``set``/``frozenset`` -> ``list[JsonValue]``
    * dataclass instance  -> ``dict[str, JsonValue]``
    * anything else       -> a :func:`fallback_marker` object

    Note what is *not* on the list: no automatic ``str()`` escape hatch, and
    ``bytes`` becomes a marker rather than an inline blob. Both choices exist so a
    lossy conversion is visible in the stored trace and so binary content never
    lands in SQLite (agent.md #16).
    """
    # Enum first: str-subclassed enums (StrEnum) would otherwise be caught below.
    if isinstance(value, Enum):
        return to_json_value(value.value)

    if isinstance(value, BaseModel):
        return to_json_value(value.model_dump(mode="json"))

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_value(item) for item in value]

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json_value(dataclasses.asdict(value))

    return fallback_marker(value)


def to_json_object(value: Mapping[str, object] | None) -> JsonObject:
    """Convert a mapping into a :data:`JsonObject`.

    Returns an empty object for ``None`` so callers never have to deal with a
    ``None`` metadata payload.
    """
    if value is None:
        return {}

    converted = to_json_value(dict(value))
    if not isinstance(converted, dict):
        raise TypeError(f"Expected a JSON object, got {type(converted).__name__}")
    return converted


def is_fallback_marker(value: object) -> bool:
    """Whether ``value`` is a degraded-payload marker produced by AER."""
    return isinstance(value, Mapping) and value.get(FALLBACK_MARKER_KEY) is True
