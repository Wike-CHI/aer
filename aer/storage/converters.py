"""Conversion helpers shared by the storage repositories.

This module owns every detail about *how a value is represented on disk*:

* JSON columns <-> :data:`~aer.runtime.serialization.JsonValue`;
* enum columns <-> :class:`enum.StrEnum` members.

Keeping that knowledge here means the domain models stay storage-agnostic and no
repository has to embed serialisation logic (a stated requirement of this
milestone: "JSON 序列化细节留在 Storage 层").
"""

from __future__ import annotations

import json
from enum import StrEnum

from aer.exceptions import StorageError
from aer.runtime.serialization import JsonObject, JsonValue


def dump_json(value: JsonValue) -> str | None:
    """Encode a JSON value for a TEXT column.

    ``None`` maps to SQL ``NULL``. ``default=str`` is a last-resort safety net:
    losing trace data is worse than storing an imperfect representation of it.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def load_json(raw: str | None) -> JsonValue:
    """Decode a TEXT column back into a JSON value."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageError(f"Stored JSON is corrupted: {exc}") from exc


def dump_json_object(value: JsonObject) -> str:
    """Encode a JSON object; empty objects are stored as ``{}`` rather than NULL."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def load_json_object(raw: str | None) -> JsonObject:
    """Decode a JSON-object column, tolerating ``NULL`` as an empty object."""
    value = load_json(raw)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StorageError(f"Expected a JSON object, found {type(value).__name__}")
    return value


def decode_enum[EnumT: StrEnum](enum_type: type[EnumT], raw: str, *, context: str) -> EnumT:
    """Turn a stored string back into an enum member.

    An unknown value means the database was written by an incompatible version or
    edited by hand, so it is reported as a :class:`StorageError` instead of being
    silently coerced.
    """
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise StorageError(f"{context} holds an unknown value {raw!r}") from exc
