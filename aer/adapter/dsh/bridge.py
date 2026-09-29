"""The DSH bridge: a long-lived process the DSH plugin talks to over stdin/stdout.

Why a process rather than an HTTP server (round-8.2 section 46): the plugin runs on the
agent's own machine, in the agent's own turn, and a socket would add a port, a
lifecycle and an authentication story to solve a problem that does not exist yet. A
second milestone can move the transport; the protocol is already explicit enough to
survive that.

Why *long-lived* (section 48): one process per plugin instance, not one per event. A
tool-heavy turn produces a hundred durable events, and a spawn each would cost more
than the work being recorded.

The loop is deliberately dumb. It parses, dispatches to a sink, and writes one response
per request. Everything that knows about AER lives behind :class:`BridgeSink`, which is
what makes this file testable without a runtime and the runtime testable without DSH.

Run it as::

    python -m aer.adapter.dsh.bridge --sink recorder --record ./dsh-bridge.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from aer.adapter.dsh.protocol import (
    AER_BRIDGE_PROTOCOL_VERSION,
    BridgeRequest,
    failed,
    ok,
)
from aer.exceptions import AdapterProtocolError, AERError

__all__ = [
    "BridgeSink",
    "RecorderSink",
    "StaticContext",
    "main",
    "serve",
]


class BridgeSink(Protocol):
    """What the loop needs from whatever is behind it.

    Every method returns the operation's result, or ``None`` when the operation has no
    result. Raising is allowed and becomes a structured error response: the plugin
    treats a failed call as a degraded bridge rather than as a failed turn.
    """

    def session_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def turn_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def event_ingest(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def turn_end(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def retrieve(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def injected(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def close(self) -> None: ...

    def observe(self, request: BridgeRequest, result: Mapping[str, Any] | None) -> None:
        """Called by the loop after every dispatch.

        A sink that records nothing need not override it.
        """
        ...


@dataclass(frozen=True, slots=True)
class StaticContext:
    """A fixed retrieval answer, used by the probe.

    Its text says what it is. A probe that injected something indistinguishable from a
    real experience would make the injection proof unfalsifiable: a reader could not
    tell whether AER's retrieval worked or a canned string was printed.
    """

    text: str
    count: int = 1


@dataclass
class RecorderSink:
    """Records every request and answers retrievals from a fixed context.

    The first bridge a new install runs, and the one the compatibility probe uses. It
    proves the transport, the event feed and the injection mechanism without requiring
    an AER deployment to exist yet -- and it is honest about that: nothing it writes is
    AER evidence.
    """

    record_path: Path | None = None
    context: StaticContext | None = None
    #: Requests received, by operation. Returned by ``ping`` so a probe can assert the
    #: plugin really sent what it claims.
    counts: dict[str, int] = field(default_factory=dict)
    _handle: TextIO | None = None

    def observe(self, request: BridgeRequest, result: Mapping[str, Any] | None) -> None:
        """Count the request, and append it to the record file when one is configured.

        Counting happens even without a file: ``ping`` reports the counts, which is how a
        probe tells "the plugin is delivering" from "the plugin loaded and then said
        nothing".
        """
        self.counts[request.operation] = self.counts.get(request.operation, 0) + 1
        if self.record_path is None:
            return
        if self._handle is None:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.record_path.open("a", encoding="utf-8")
        self._handle.write(
            json.dumps(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "operation": request.operation,
                    "request": request.payload,
                    "result": result,
                },
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
        self._handle.flush()

    # -- operations --------------------------------------------------------

    def session_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return None

    def turn_begin(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return None

    def event_ingest(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return None

    def turn_end(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return None

    def retrieve(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if self.context is None:
            # Nothing to inject is a normal, expected answer, and it is what keeps the
            # plugin's "no context" path exercised on every run.
            return {"retrieval_id": "none", "context_text": "", "experience_count": 0}
        return {
            "retrieval_id": f"probe-{self.counts.get('experience.retrieve', 0)}",
            "context_text": self.context.text,
            "experience_count": self.context.count,
        }

    def injected(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return None

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


_DISPATCH = {
    "session.begin": "session_begin",
    "turn.begin": "turn_begin",
    "event.ingest": "event_ingest",
    "turn.end": "turn_end",
    "experience.retrieve": "retrieve",
    "experience.injected": "injected",
}


def serve(stdin: Iterable[str], stdout: TextIO, sink: BridgeSink, *, verbose: bool = False) -> int:
    """Read requests until EOF or ``shutdown``, writing one response each.

    Returns a process exit code. A malformed line is answered with an error and the
    loop continues: the plugin should learn that one request was rejected, not lose the
    rest of a session because a single line was corrupted.

    ``shutdown`` is answered *before* the sink closes, so a caller that asked for an
    orderly stop can rely on the response arriving.
    """
    for raw in stdin:
        line = raw.strip()
        if line == "":
            continue
        try:
            request = BridgeRequest.parse(line)
        except AdapterProtocolError as exc:
            # No request_id is available, so the response cannot be correlated; the
            # plugin will time that request out and count it as failed.
            stdout.write(failed("unknown", "BRIDGE_PROTOCOL_ERROR", str(exc)) + "\n")
            stdout.flush()
            continue

        if request.operation == "shutdown":
            stdout.write(ok(request.request_id, {"stopped": True}) + "\n")
            stdout.flush()
            break
        if request.operation == "ping":
            counts = getattr(sink, "counts", {})
            stdout.write(
                ok(
                    request.request_id,
                    {"protocol": AER_BRIDGE_PROTOCOL_VERSION, "counts": dict(counts)},
                )
                + "\n"
            )
            stdout.flush()
            continue

        method_name = _DISPATCH.get(request.operation)
        try:
            if method_name is None:  # pragma: no cover - guarded by BRIDGE_OPERATIONS
                raise AdapterProtocolError(f"no handler for {request.operation!r}")
            if verbose:
                sys.stderr.write(f"[aer-dsh-bridge] {request.operation}\n")
            result = getattr(sink, method_name)(request.payload)
        except AERError as exc:
            stdout.write(failed(request.request_id, type(exc).__name__, str(exc)) + "\n")
            result = None
        except Exception as exc:
            stdout.write(failed(request.request_id, "BRIDGE_INTERNAL_ERROR", str(exc)) + "\n")
            result = None
        else:
            stdout.write(ok(request.request_id, result) + "\n")
        # Recording is best-effort: a sink that cannot write must not turn an applied
        # request into an error the plugin would retry into a duplicate.
        try:
            observe = getattr(sink, "observe", None)
            if callable(observe):
                observe(request, result)
        except Exception as exc:
            sys.stderr.write(f"[aer-dsh-bridge] observe failed: {exc}\n")
        stdout.flush()

    sink.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point used by the DSH plugin and by the probe."""
    parser = argparse.ArgumentParser(prog="aer.adapter.dsh.bridge")
    parser.add_argument(
        "--record",
        help="Append every request to this JSON Lines file.",
    )
    parser.add_argument(
        "--context",
        help=(
            "Answer retrievals with this fixed context text. Used by the probe to prove "
            "that a plugin-sourced context message reaches the model request; it is not "
            "AER experience and must not be presented as such."
        ),
    )
    parser.add_argument("--context-count", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    context = (
        None if args.context is None else StaticContext(text=args.context, count=args.context_count)
    )
    sink = RecorderSink(
        record_path=None if args.record is None else Path(args.record),
        context=context,
    )
    return serve(sys.stdin, sys.stdout, sink, verbose=args.verbose)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
