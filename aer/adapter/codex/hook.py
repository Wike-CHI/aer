"""The hook command Codex runs: stdin JSON in, one AER delivery out.

    codex hook event  ->  stdin JSON  ->  this module  ->  AdapterIngestor  ->  AER

Installed by pointing Codex at it::

    python -m aer.adapter.codex.hook

Two properties matter more than anything else here.

**It is fast, and it is allowed to fail.** Section 8 rules out running distillation,
rebuilds or analytics on the hook's synchronous path, and the probe established what
happens when a hook misbehaves: Codex prints ``hook: <Event> Failed``, does **not**
retry, and carries on. That is the behaviour this command depends on -- a broken AER
must never block the agent it is observing, so every failure exits non-zero with a
diagnosis on stderr and nothing else.

**It decides nothing about the task.** The run is opened from the first payload that can
describe what was asked (a prompt), not from ``SessionStart``, which carries no task at
all; and the run is finished only by ``SessionEnd``, which is the single terminal
authority. Both are consequences of what the CLI was measured to send -- see
``docs/DECISIONS.md`` D-091 and D-093.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence

from aer.adapter.codex import mapping
from aer.adapter.codex.adapter import CodexAdapter
from aer.adapter.codex.coverage import CodexMode
from aer.config import load_deployment_config
from aer.exceptions import AdapterSessionTerminated, AERError
from aer.runtime.runtime import AER

__all__ = ["main"]

#: Events that establish a run. Only a payload carrying a prompt can, because Codex's
#: ``SessionStart`` has no task field (captured, not assumed).
_OPENS_A_RUN = frozenset({mapping.CodexHookEvent.USER_PROMPT_SUBMIT})

#: Exit codes. ``0`` means "Codex should carry on as if nothing happened"; anything else
#: is reported by Codex as a failed hook, which is what an operator needs to see.
_EXIT_OK = 0
_EXIT_PROBLEM = 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one hook delivery. Returns the process exit code.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        ``0`` when the delivery was handled or deliberately skipped, non-zero when it
        could not be: Codex reports the latter and continues, so the agent is never
        blocked by AER's problems.
    """
    parser = argparse.ArgumentParser(
        prog="python -m aer.adapter.codex.hook",
        description="Receive one Codex lifecycle hook and record it in AER.",
    )
    parser.add_argument(
        "--print-coverage",
        action="store_true",
        help="Print what this adapter has been verified to observe, then exit.",
    )
    parser.add_argument(
        "--print-delivery",
        action="store_true",
        help="Print the AER outcome of the delivery to stderr (for setup debugging).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    adapter = CodexAdapter()

    if args.print_coverage:
        print(adapter.coverage.describe(CodexMode.EXEC))
        print()
        print(adapter.coverage.describe(CodexMode.INTERACTIVE))
        return _EXIT_OK

    try:
        payload = _read_payload()
    except ValueError as exc:
        return _fail(f"could not read a Codex hook payload from stdin: {exc}")

    try:
        return _deliver(payload, adapter, verbose=args.print_delivery)
    except AdapterSessionTerminated as exc:
        # A session whose run already finished, reconnecting. Not an error the agent
        # should see: the trace is intact, there is simply nothing left to record.
        return _note(f"session already closed: {exc}")
    except AERError as exc:
        return _fail(f"AER refused this delivery: {exc}")
    except Exception as exc:
        return _fail(f"unexpected failure ({type(exc).__name__}: {exc})")


def _deliver(payload: Mapping[str, object], adapter: CodexAdapter, *, verbose: bool) -> int:
    """Route one payload to the runtime. Split out so :func:`main` stays readable."""
    event = mapping.event_name(payload)
    session = mapping.session_id(payload)
    config = load_deployment_config()

    with AER(config.data_dir, db_filename=config.db_path.name) as runtime:
        ingestor = runtime.adapter_ingestor
        existing = runtime.find_adapter_session(mapping.CODEX_PROVIDER, session)

        if event is mapping.CodexHookEvent.SESSION_START:
            # A session began. If it is a known session this is a resume; either way
            # there is no task to open a run with, so the delivery ends here and the
            # coverage of "we saw a session start" is the value it adds.
            state = "resumed" if existing is not None else "new"
            return _note(
                f"Codex session {session} started ({state}, source="
                f"{payload.get('source')!r}); awaiting a prompt before opening a run"
            )

        if existing is None and event not in _OPENS_A_RUN:
            # A tool event before any prompt. There is no task to attach it to, and
            # inventing one would label the whole run with a placeholder.
            return _note(
                f"no AER run for Codex session {session} yet; {event.value} arrives before "
                "any prompt and is not recorded"
            )

        if event is mapping.CodexHookEvent.SESSION_END:
            return _finish(ingestor, adapter, payload, session, verbose=verbose)

        handle = ingestor.open(adapter, payload)
        result = ingestor.ingest(handle, payload)
        if verbose:
            print(
                f"[aer-codex] {event.value} -> {result.outcome.value} "
                f"run={result.run_id} envelopes={result.envelopes}",
                file=sys.stderr,
            )
        return _EXIT_OK


def _finish(
    ingestor,
    adapter: CodexAdapter,
    payload: Mapping[str, object],
    session: str,
    *,
    verbose: bool,
) -> int:
    """Close the run for a session.

    ``SessionEnd`` is the only event that finishes a run (D-093). Which status that
    produces is the adapter's decision, because it depends on what the payload states;
    the hook's job is only to route the delivery.
    """
    handle = ingestor.open(adapter, payload)
    result = ingestor.close(handle, payload)
    if verbose:
        state = "already finished" if result is None else result.status.value
        print(
            f"[aer-codex] SessionEnd (reason={payload.get('reason')!r}) -> {state}",
            file=sys.stderr,
        )
    return _EXIT_OK


def _read_payload() -> Mapping[str, object]:
    """Read the hook payload Codex writes to stdin.

    Raises:
        ValueError: stdin was empty or not a JSON object. Both mean the command was not
            invoked by Codex, which is a setup mistake worth reporting rather than
            guessing around.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin was empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"stdin held a {type(parsed).__name__}, expected a JSON object")
    return parsed


def _note(message: str) -> int:
    """Report a deliberate skip. Not an error: Codex should not log a failure."""
    print(f"[aer-codex] {message}", file=sys.stderr)
    return _EXIT_OK


def _fail(message: str) -> int:
    """Report a delivery AER could not handle, and let Codex carry on."""
    print(f"[aer-codex] {message}", file=sys.stderr)
    return _EXIT_PROBLEM


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
