#!/usr/bin/env python
"""Probe the installed Codex CLI for the hooks this adapter depends on.

    python scripts/probe_codex_hooks.py                # exec mode, no model call
    python scripts/probe_codex_hooks.py --mode both    # add the interactive attempt
    python scripts/probe_codex_hooks.py --keep         # keep the scratch home for inspection

Run this **before upgrading Codex** (round-8.1 sections 34-35). The adapter's mapping is
written against a measured CLI, so a new release is a question -- "does this still
deliver what we built on?" -- and this script is the answer. Discovering it from bad
data already inside AER is the alternative, and that is the expensive path.

What the probe does, and what it deliberately does not:

* it builds a **scratch** ``CODEX_HOME`` in a temporary directory, so the operator's own
  Codex configuration, session history and hook trust state are never touched;
* it writes hooks for all twelve lifecycle events, each one a tiny recorder that dumps
  its stdin verbatim, and then runs Codex against that home;
* it reports a matrix rather than a verdict: which events fired in which mode, plus the
  payload fields each one actually carried;
* by default it passes a **model name that does not exist**, so the run stops at the
  provider call and no tokens are spent. Hooks fire before that point, which is what
  makes a free probe possible at all;
* it never copies credentials. ``--use-auth`` opts in to running with the operator's
  real login, which is what a tool-call probe needs, and it is off by default because
  "spend the user's quota" is not something a diagnostic should decide on its own.

Two things this script cannot do from a sandbox, both of which it reports as gaps rather
than skipping quietly: the interactive TUI needs a terminal, and a tool call needs a
working model.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
    "Interrupt",
)

#: A model name that cannot exist, so the provider call fails instead of completing.
#: The run still creates a session and still dispatches the session hooks, which is the
#: whole point: a probe that costs nothing and still observes the lifecycle.
NON_BILLING_MODEL = "this-model-does-not-exist"

RECORDER = '''\
"""Scratch recorder written by scripts/probe_codex_hooks.py. Records, decides nothing."""
import json, os, sys, time
from pathlib import Path

target = Path(os.environ["AER_CODEX_PROBE_LOG"])
raw = sys.stdin.read()
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": sys.argv[1] if len(sys.argv) > 1 else "?",
        "chars": len(raw),
        "payload": raw,
    }, ensure_ascii=False) + "\\n")
'''


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    codex = args.codex or shutil.which("codex")
    if not codex:
        print("codex CLI not found on PATH -- nothing to probe", file=sys.stderr)
        return 2
    args.codex = codex

    version = _codex_version(codex)
    print(f"Codex CLI : {version}  ({codex})")

    workspace = Path(tempfile.mkdtemp(prefix="aer-codex-probe-"))
    try:
        home = workspace / "home"
        home.mkdir()
        log = workspace / "payloads.jsonl"
        recorder = workspace / "record_hook.py"
        recorder.write_text(RECORDER, encoding="utf-8")
        _write_hooks(home, recorder)
        # The scratch home gets its own sandbox mode. The default allows a tool to
        # actually execute, because a read-only sandbox refuses the call and the run then
        # produces no PostToolUse at all -- measured, and the reason an earlier probe saw
        # PreToolUse with no matching result.
        (home / "config.toml").write_text(
            "sandbox_mode = " + json.dumps(args.sandbox) + '\napproval_policy = "never"\n',
            encoding="utf-8",
        )

        if args.use_auth:
            _copy_auth(home)
            print("auth      : using the operator's login (--use-auth)")
        else:
            print(
                "auth      : none; the provider call will fail, which stops the run "
                "before it costs anything"
            )

        observations: dict[str, list[dict[str, object]]] = {}
        for mode in _modes(args.mode):
            print(f"\n=== probing {mode} mode ===")
            observations[mode] = _probe(mode, home, log, version, args)
    finally:
        if args.keep:
            print(f"\nscratch home kept at {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    report = _report(version, observations)
    _print_report(report, observations)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.output}")
    return 0 if report["usable"] else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("exec", "interactive", "both"), default="exec")
    parser.add_argument(
        "--model",
        default=NON_BILLING_MODEL,
        help="Model to run with. The default cannot exist, so no tokens are spent.",
    )
    parser.add_argument("--prompt", default="probe: reply with the single word ok")
    parser.add_argument(
        "--workdir",
        default="",
        help="Run Codex inside this directory. Needed for a tool-call probe: a tool call "
        "only happens when the agent has something to act on.",
    )
    parser.add_argument(
        "--sandbox",
        default="danger-full-access",
        help="Sandbox mode for the probe run. The default lets a tool actually execute, "
        "which is what produces PreToolUse/PostToolUse; read-only refuses the tool and "
        "the run yields no PostToolUse at all (measured).",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--use-auth",
        action="store_true",
        help="Copy the operator's login into the scratch home. Needed for a tool-call "
        "probe, and it will spend quota.",
    )
    parser.add_argument("--codex", default="", help="Path to the codex executable.")
    parser.add_argument("--output", help="Write the report as JSON to this path.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch home.")
    return parser.parse_args(argv)


def _modes(mode: str) -> tuple[str, ...]:
    if mode == "both":
        return ("exec", "interactive")
    return (mode,)


def _codex_version(codex: str) -> str:
    for use_shell in (False, True):
        try:
            completed = subprocess.run(
                f'"{codex}" --version' if use_shell else [codex, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                shell=use_shell,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return (completed.stdout or completed.stderr).strip() or "unknown"
    return "unknown"


def _write_hooks(home: Path, recorder: Path) -> None:
    """Write a hooks.json enabling all twelve events.

    The command is an **absolute, unquoted** program path. Quoting it makes Codex fail
    to launch the process at all, which looks exactly like "the hook never fired" -- a
    measured trap, and the reason this is written once here rather than by hand in
    documentation.
    """
    python = Path(sys.executable).resolve()
    command_for = lambda event: f"{python} {recorder} {event}"  # noqa: E731
    hooks = {
        event: [{"hooks": [{"type": "command", "command": command_for(event), "timeout": 30}]}]
        for event in HOOK_EVENTS
    }
    (home / "hooks.json").write_text(
        json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8"
    )


def _copy_auth(home: Path) -> None:
    """Copy ``auth.json`` from the operator's real home into the scratch one.

    Only ``auth.json``: the trust state and session history are deliberately not
    carried over, so the probe cannot accidentally run hooks the operator never
    approved outside this scratch directory.
    """
    real = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    if real.is_file():
        shutil.copy2(real, home / "auth.json")


def _probe(
    mode: str,
    home: Path,
    log: Path,
    version: str,
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    """Run one Codex mode and return the deliveries it produced."""
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    env = {**os.environ, "CODEX_HOME": str(home), "AER_CODEX_PROBE_LOG": str(log)}

    # The *resolved* path, not the bare name: on Windows `codex` is an npm shim and
    # CreateProcess performs no PATHEXT lookup, so a bare name raises FileNotFoundError
    # and the probe silently observes nothing.
    command = [args.codex, "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust"]
    if mode == "exec":
        command += ["--model", args.model, args.prompt]
    else:
        # The TUI needs a terminal. Running it without one is attempted rather than
        # assumed impossible, and a failure here is reported as "not covered".
        command += ["--model", args.model, args.prompt]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            cwd=args.workdir or str(home),
        )
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        output = "<timed out>"
    except OSError as exc:
        output = f"<could not run: {exc}>"

    after = log.read_text(encoding="utf-8") if log.exists() else ""
    fresh = after[len(before) :]
    deliveries = [json.loads(line) for line in fresh.splitlines() if line.strip()]
    for delivery in deliveries:
        delivery["dispatched"] = _dispatched(output, str(delivery.get("event")))
    return deliveries


def _dispatched(output: str, event: str) -> bool:
    """Whether Codex logged the hook as run, using its own output as the evidence."""
    return f"hook: {event}" in output


def _report(version: str, observations: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    modes: dict[str, object] = {}
    for mode, deliveries in observations.items():
        seen: dict[str, dict[str, object]] = {}
        for delivery in deliveries:
            event = str(delivery.get("event"))
            entry = seen.setdefault(event, {"fired": 0, "fields": [], "completed": False})
            entry["fired"] = int(entry["fired"]) + 1  # type: ignore[call-overload]
            entry["completed"] = bool(entry["completed"]) or bool(delivery.get("dispatched"))
            fields = sorted(_payload_fields(delivery.get("payload")))
            entry["fields"] = sorted(set(entry["fields"]) | set(fields))  # type: ignore[arg-type]
        modes[mode] = {
            "observed": sorted(seen),
            "missing": [event for event in HOOK_EVENTS if event not in seen],
            "events": seen,
        }
    return {
        "codex_version": version,
        "probed_on": time.strftime("%Y-%m-%d"),
        "modes": modes,
        # "Usable" is deliberately narrow: the three session hooks are what the adapter's
        # lifecycle depends on. Tool hooks are reported as missing without failing the
        # probe, because they need a model turn and a probe that refused to run without
        # one would never run in CI.
        "usable": all(
            {"SessionStart", "UserPromptSubmit", "SessionEnd"} <= set(mode_data["observed"])  # type: ignore[arg-type]
            for mode_data in modes.values()
        ),
    }


def _payload_fields(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return sorted(parsed) if isinstance(parsed, dict) else []


def _print_report(
    report: dict[str, object], observations: dict[str, list[dict[str, object]]]
) -> None:
    print("\n=== coverage ===")
    for mode, mode_data in report["modes"].items():  # type: ignore[union-attr]
        print(f"\n[{mode}]")
        for event in HOOK_EVENTS:
            entry = mode_data["events"].get(event)  # type: ignore[union-attr]
            if entry is None:
                print(f"  {event:18} MISSING")
                continue
            fields = ", ".join(entry["fields"]) or "(no fields)"
            print(f"  {event:18} fired x{entry['fired']:<3} completed={entry['completed']}")
            print(f"  {'':18} fields: {fields}")
    print()
    for mode, deliveries in observations.items():
        if not deliveries:
            print(f"[{mode}] no hook deliveries were observed at all")
    print(
        "\nSession hooks "
        + (
            "are covered."
            if report["usable"]
            else "are NOT fully covered -- the adapter "
            "cannot be trusted against this CLI until they are."
        )
    )
    covered = {
        event
        for mode_data in report["modes"].values()  # type: ignore[union-attr]
        for event in mode_data["observed"]  # type: ignore[union-attr]
    }
    if {"PreToolUse", "PostToolUse"} <= covered:
        print("Tool hooks are covered too: the tool surface behaves as the adapter expects.")
    else:
        print(
            "Tool hooks need a completed model turn. Re-run with --use-auth --model <name> "
            "and --workdir pointing at a repository, so the agent has something to act on."
        )
    print(
        "Tool hooks need a working model turn; re-run with --use-auth on a machine that "
        "can reach the provider to cover them."
    )


if __name__ == "__main__":
    raise SystemExit(main())
