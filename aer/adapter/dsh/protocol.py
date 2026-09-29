"""The DSH bridge protocol, in Python.

The mirror of ``integrations/dsh/src/protocol.ts``. Both sides are hand-written from
the same small vocabulary, so the two files must be read together: this module is the
*only* place the Python side learns what the plugin sends, and it validates rather
than trusts, because the plugin is a separate process written in a different language
squarely in the "untrusted input" category that M8's sanitisation already covers.

Framing is one JSON object per line in each direction (round-8.2 section 47).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from aer.exceptions import AdapterProtocolError
from aer.runtime.serialization import JsonObject, to_json_object

#: Bumped in lockstep with ``AER_BRIDGE_PROTOCOL_VERSION`` on the TypeScript side.
AER_BRIDGE_PROTOCOL_VERSION: Final = 1

#: Operations the plugin may send. Anything else is a protocol error, not a no-op.
BRIDGE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "session.begin",
        "turn.begin",
        "event.ingest",
        "turn.end",
        "experience.retrieve",
        "experience.injected",
        "ping",
        "shutdown",
    }
)


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    """One request from the plugin."""

    protocol: int
    request_id: str
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, line: str) -> BridgeRequest:
        """Parse one wire line.

        Raises:
            AdapterProtocolError: the line is not a JSON object, is missing a field,
                names an unknown operation, or was written by another protocol
                version. All four are integration errors and none of them may be
                silently absorbed: a bridge that guesses at a malformed request writes
                evidence nobody can trace back to a source.
        """
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterProtocolError(f"bridge request is not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise AdapterProtocolError(
                f"bridge request must be an object, got {type(raw).__name__}"
            )

        protocol = raw.get("protocol")
        if protocol != AER_BRIDGE_PROTOCOL_VERSION:
            raise AdapterProtocolError(
                f"bridge protocol mismatch: plugin sent {protocol!r}, "
                f"this bridge speaks {AER_BRIDGE_PROTOCOL_VERSION}"
            )
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise AdapterProtocolError("bridge request has no request_id")
        operation = raw.get("operation")
        if operation not in BRIDGE_OPERATIONS:
            raise AdapterProtocolError(
                f"unknown bridge operation {operation!r}; "
                f"expected one of {sorted(BRIDGE_OPERATIONS)}"
            )
        payload = raw.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise AdapterProtocolError("bridge request payload must be an object")
        return cls(
            protocol=protocol,
            request_id=request_id,
            operation=str(operation),
            payload=payload,
        )

    def object(self, key: str, *, required: bool = True) -> JsonObject | None:
        """One nested object from the payload, validated.

        Returns ``None`` when an optional field is absent, and raises when a required
        one is missing or the wrong shape. The distinction is deliberate: an absent
        optional field is normal (a tool result with no structured error), while an
        absent required one means the two sides disagree about the protocol.
        """
        value = self.payload.get(key)
        if value is None:
            if required:
                raise AdapterProtocolError(f"{self.operation} requires a {key!r} object")
            return None
        if not isinstance(value, dict):
            raise AdapterProtocolError(f"{self.operation} field {key!r} must be an object")
        return to_json_object(value)


def ok(request_id: str, result: Mapping[str, Any] | None = None) -> str:
    """A successful response line."""
    body: dict[str, Any] = {
        "protocol": AER_BRIDGE_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
    }
    if result is not None:
        body["result"] = dict(result)
    return json.dumps(body, ensure_ascii=False, default=str)


def failed(request_id: str, code: str, message: str) -> str:
    """A failed response line. ``code`` is stable; ``message`` is for a human."""
    return json.dumps(
        {
            "protocol": AER_BRIDGE_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        },
        ensure_ascii=False,
        default=str,
    )
