"""The hook command Claude Code runs: stdin JSON in, one AER delivery out.

    Claude Code hook event  ->  stdin JSON  ->  this module  ->  AdapterIngestor  ->  AER

Installed by pointing Claude Code's ``settings.json`` hooks at it::

    python -m aer.adapter.claude.hook

Three properties matter more than anything here.

**It is fast, and it is allowed to fail.** Round-8 section 8 rules out running
distillation, rebuilds or analytics on the hook's synchronous path, and a failing
hook is reported by Claude Code and *blocks the event* -- so every failure exits
non-zero with a diagnosis on stderr and nothing else. A broken AER must never
block the agent it is observing.

**stdout is a protocol surface, not a log.** Claude Code parses hook stdout as
JSON to decide what to do next (permission decisions, updated input, injected
context). Everything this command says for humans goes to stderr; stdout stays
empty unless a future capability deliberately returns a JSON decision.

**It decides nothing about the task.** Runs open from ``UserPromptSubmit`` (the
first payload that says what was asked; ``SessionStart`` carries no task, D-091),
close only on the run-closing events declared in
:mod:`~aer.adapter.claude.mapping`, and every close is ``INCONCLUSIVE`` because no
Claude Code hook declares a task outcome (round-8.1.1, D-100). A ``Stop`` never
terminates a run: it is turn-level (D-098).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from aer.adapter.claude import mapping
from aer.adapter.claude.adapter import ClaudeCodeAdapter
from aer.adapter.claude.coverage import ClaudeMode
from aer.config import load_deployment_config
from aer.exceptions import AdapterSessionTerminated, AERError
from aer.runtime.runtime import AER

__all__ = ["main"]

#: Events that establish a run. Only a payload carrying a prompt can, because
#: Claude Code's ``SessionStart`` has no task field (captured from the binary's
#: vocabulary, not assumed).
_OPENS_A_RUN = frozenset({mapping.ClaudeHookEvent.USER_PROMPT_SUBMIT})

#: Exit codes. ``0`` means "Claude Code should carry on as if nothing happened";
#: anything else marks the hook as failed, which is what an operator needs to see.
_EXIT_OK = 0
_EXIT_PROBLEM = 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one hook delivery. Returns the process exit code.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        ``0`` when the delivery was handled or deliberately skipped, non-zero when
        it could not be: Claude Code reports the failure and continues, so the agent
        is never blocked by AER's problems for more than this one event.
    """
    parser = argparse.ArgumentParser(
        prog="python -m aer.adapter.claude.hook",
        description="Receive one Claude Code lifecycle hook and record it in AER.",
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
    parser.add_argument(
        "--capture",
        metavar="DIR",
        default=None,
        help="Write the raw payload as JSON into DIR before processing (probe use).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    adapter = ClaudeCodeAdapter()

    if args.print_coverage:
        print(adapter.coverage.describe(ClaudeMode.PRINT))
        print()
        print(adapter.coverage.describe(ClaudeMode.INTERACTIVE))
        return _EXIT_OK

    try:
        payload = _read_payload()
    except ValueError as exc:
        return _fail(f"could not read a Claude Code hook payload from stdin: {exc}")

    if args.capture:
        _capture_payload(Path(args.capture), payload)

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


def _deliver(payload: Mapping[str, object], adapter: ClaudeCodeAdapter, *, verbose: bool) -> int:
    """Route one payload to the runtime. Split out so :func:`main` stays readable."""
    event = mapping.event_name(payload)
    session = mapping.session_id(payload)
    config = load_deployment_config()

    with AER(config.data_dir, db_filename=config.db_path.name) as runtime:
        ingestor = runtime.adapter_ingestor
        existing = runtime.find_adapter_session(mapping.CLAUDE_PROVIDER, session)

        if event is mapping.ClaudeHookEvent.SESSION_START:
            # A session began. If it is a known session this is a resume; either way
            # there is no task to open a run with, so the delivery ends here and the
            # coverage of "we saw a session start" is the value it adds.
            state = "resumed" if existing is not None else "new"
            return _note(
                f"Claude Code session {session} started ({state}, source="
                f"{payload.get('source')!r}); awaiting a prompt before opening a run"
            )

        if event is mapping.ClaudeHookEvent.SESSION_END:
            if existing is None:
                return _note(
                    f"no AER run for Claude Code session {session}; SessionEnd "
                    "arrived before any prompt and there is nothing to close"
                )
            return _finish(ingestor, adapter, payload, session, verbose=verbose)

        if event not in _OPENS_A_RUN and existing is None:
            # A tool event before any prompt. There is no task to attach it to, and
            # inventing one would label the whole run with a placeholder.
            return _note(
                f"no AER run for Claude Code session {session} yet; {event.value} "
                "arrives before any prompt and is not recorded"
            )

        if event in _OPENS_A_RUN:
            # A turn is the unit of work, not the session: if the previous turn's run
            # is still open, the new prompt ends it (INCONCLUSIVE -- nobody declared
            # how the previous task went) before the new run opens.
            if existing is not None:
                try:
                    previous = ingestor.open(adapter, payload)
                    ingestor.close(previous, payload)
                except AdapterSessionTerminated:
                    pass  # already terminal; reopen() below starts the new episode
                handle = ingestor.reopen(adapter, payload)
            else:
                handle = ingestor.open(adapter, payload)
            if verbose:
                print(
                    f"[aer-claude-code] {event.value} -> {handle.outcome.value} "
                    f"run={handle.run_id}",
                    file=sys.stderr,
                )
            return _EXIT_OK

        handle = ingestor.open(adapter, payload)
        result = ingestor.ingest(handle, payload)
        if verbose:
            print(
                f"[aer-claude-code] {event.value} -> {result.outcome.value} "
                f"run={result.run_id} envelopes={result.envelopes}",
                file=sys.stderr,
            )
        return _EXIT_OK


def _finish(
    ingestor,
    adapter: ClaudeCodeAdapter,
    payload: Mapping[str, object],
    session: str,
    *,
    verbose: bool,
) -> int:
    """Close the run for a session.

    ``SessionEnd`` is the only event that finishes a run (D-101: ``Teardown`` is
    not a hook event), and what it produces is always ``INCONCLUSIVE`` (D-100):
    it states why the session ended, not how the task went. The hook's job is
    only to route the delivery.
    """
    handle = ingestor.open(adapter, payload)
    run = ingestor.close(handle, payload)
    if verbose:
        state = "already finished" if run is None else run.status.value
        print(
            f"[aer-claude-code] {payload.get('hook_event_name')} "
            f"(reason={payload.get('reason')!r}) -> {state}",
            file=sys.stderr,
        )
    return _EXIT_OK


def _read_payload() -> Mapping[str, object]:
    """Read the hook payload Claude Code writes to stdin.

    Raises:
        ValueError: stdin was empty or not a JSON object. Both mean the command was
            not invoked by Claude Code, which is a setup mistake worth reporting
            rather than guessing around.
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


def _capture_payload(directory: Path, payload: Mapping[str, object]) -> None:
    """Persist the raw payload for the probe (``--capture DIR``).

    One file per delivery, named ``<event>-<digest>.json`` so repeats never
    overwrite each other; the probe reads the directory and upgrades the coverage
    matrix with what it finds.
    """
    import hashlib

    directory.mkdir(parents=True, exist_ok=True)
    event = str(payload.get("hook_event_name", "unknown"))
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    target = directory / f"{event}-{digest}.json"
    if not target.exists():
        target.write_text(body, encoding="utf-8")


def _note(message: str) -> int:
    """Report a deliberate skip. Not an error: Claude Code should not log a failure."""
    print(f"[aer-claude-code] {message}", file=sys.stderr)
    return _EXIT_OK


def _fail(message: str) -> int:
    """Report a delivery AER could not handle, and let Claude Code carry on."""
    print(f"[aer-claude-code] {message}", file=sys.stderr)
    return _EXIT_PROBLEM


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
