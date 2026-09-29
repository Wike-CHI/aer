"""AER's adapter for the DeepSeek Harness.

Two halves, in two languages, with one protocol between them:

```text
integrations/dsh/          TypeScript: a native DSH Cordis plugin
    session/event          durable execution facts, once, post-commit
    agent/pre-step         retrieval + context injection into the model request
        |
        |  JSON Lines over stdin/stdout

aer/adapter/dsh/           Python: the bridge and the AER-facing adapter
    protocol.py            the wire vocabulary, validated rather than trusted
    bridge.py              the long-lived process and its sink seam
```

The split is the point of milestone M8: vendor knowledge stays in the plugin, AER
knowledge stays here, and neither side has to understand the other's types. The
TypeScript half can be tested without Python and this half without DSH.

The bridge is *not* a shell-hook bridge. DSH has a typed execution pipeline and a
durable session log, so AER subscribes to those directly rather than being invoked
once per hook with a blob of JSON (round-8.2 section 3).

This package deliberately re-exports only the protocol. Importing the bridge from here
would make ``python -m aer.adapter.dsh.bridge`` a second import of an already-imported
module, which Python warns about -- and that warning would land on the bridge's stderr,
where the plugin collects it as diagnostics. Import the bridge from
``aer.adapter.dsh.bridge`` when you want it; the process entry point needs it to stay
un-imported.
"""

from __future__ import annotations

from aer.adapter.dsh.protocol import (
    AER_BRIDGE_PROTOCOL_VERSION,
    BRIDGE_OPERATIONS,
    BridgeRequest,
    failed,
    ok,
)

__all__ = [
    "AER_BRIDGE_PROTOCOL_VERSION",
    "BRIDGE_OPERATIONS",
    "BridgeRequest",
    "failed",
    "ok",
]
