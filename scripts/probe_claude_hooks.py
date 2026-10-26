#!/usr/bin/env python
"""Probe the installed Claude Code CLI for the hooks this adapter depends on.

    python scripts/probe_claude_hooks.py                # free: no model call
    python scripts/probe_claude_hooks.py --use-auth     # one real turn (spends quota)

Run this **before upgrading Claude Code**. The adapter's event vocabulary was
extracted from claude.exe 2.1.310's string table, and every payload *shape* is only
tolerated until a real delivery confirms it -- this script is what confirms or
contradicts it. A ``CONTRACT_ONLY`` cell that turns out wrong is exactly what the
Codex adapter's D-097 records: the mapping looked tested and had never met a real
event.

What the probe does, and what it deliberately does not:

* it layers a **scratch** settings file via ``claude --settings``, so the operator's
  own settings.json, hook trust state and sessions are never touched;
* it writes hooks for all sixteen lifecycle events, each one a tiny recorder that
  dumps its stdin verbatim, then runs ``claude -p`` against that layer;
* it reports a matrix rather than a verdict: which events fired, plus the payload
  fields each one actually carried;
* by default it passes a **model name that does not exist**, so the provider call
  fails and no tokens are spent. Session-level hooks still fire before that point.
  Tool hooks need a real turn: ``--use-auth`` opts in, and it is off by default
  because "spend the user's quota" is not something a diagnostic should decide on
  its own.

One thing this script cannot do from a sandbox, and reports as a gap rather than
skipping quietly: the interactive TUI needs a terminal.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_EVENTS = (
    # The vocabulary the installed CLI's own validator printed when this probe first
    # ran (2.1.310). "Teardown" is deliberately absent: the CLI rejected it as an
    # unknown hook event -- the string table had named it, the validator did not, and
    # the validator is the authority. "PostToolBatch" is the spelling the validator
    # accepts. Both corrections are recorded as D-101.
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PreModelSwitch",
    "PostModelSwitch",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
)

#: A model name that cannot exist, so the provider call fails instead of completing.
#: Session hooks still dispatch before that point, which is what makes a free probe
#: possible at all.
NON_BILLING_MODEL = "this-model-does-not-exist"

RECORDER = '''\
"""Scratch recorder written by scripts/probe_claude_hooks.py. Records, decides nothing."""
import json, os, sys, time
from pathlib import Path

target = Path(os.environ["AER_CLAUDE_PROBE_LOG"])
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
    claude = args.claude or shutil.which("claude")
    if not claude:
        print("claude CLI not found on PATH -- nothing to probe", file=sys.stderr)
        return 2
    args.claude = claude

    version = _claude_version(claude)
    print(f"Claude Code: {version}  ({claude})")

    workspace = Path(tempfile.mkdtemp(prefix="aer-claude-probe-"))
    try:
        log = workspace / "payloads.jsonl"
        recorder = workspace / "record_hook.py"
        recorder.write_text(RECORDER, encoding="utf-8")
        settings = workspace / "settings.json"
        _write_settings(settings, recorder)

        print(f"settings  : scratch layer at {settings} (operator config untouched)")
        if args.use_auth:
            print("auth      : the operator's login (--use-auth); one real turn, quota is spent")
        else:
            print(
                "auth      : none; the provider call will fail, which stops the run "
                "before it costs anything (SessionStart/UserPromptSubmit still fire)"
            )

        observations = _probe(workspace, log, version, args)
    finally:
        if args.keep:
            print(f"\nscratch workspace kept at {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    report = _report(version, observations)
    _print_report(report, observations)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.output}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        default=NON_BILLING_MODEL,
        help="Model for the free probe; the default cannot exist, so no tokens are "
        "spent. Ignored with --use-auth, which uses the configured default model.",
    )
    parser.add_argument(
        "--prompt",
        default="Use the Bash tool to run `echo probe-ok`, then use the Read tool to "
        "read the file `probe.txt` if it exists, then reply with the single word ok.",
    )
    parser.add_argument(
        "--workdir",
        default="",
        help="Run Claude Code inside this directory (defaults to the scratch workspace).",
    )
    parser.add_argument(
        "--use-auth",
        action="store_true",
        help="Run with the operator's real login. Needed for tool-hook coverage "
        "(PreToolUse/PostToolUse), and it will spend quota.",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--claude", default="", help="Path to the claude executable.")
    parser.add_argument("--output", help="Write the report as JSON to this path.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch workspace.")
    return parser.parse_args(argv)


def _claude_version(claude: str) -> str:
    for use_shell in (False, True):
        try:
            completed = subprocess.run(
                f'"{claude}" --version' if use_shell else [claude, "--version"],
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


def _write_settings(settings: Path, recorder: Path) -> None:
    """Write the scratch settings layer enabling all sixteen events.

    The command is an **absolute, unquoted** program path: Claude Code parses the
    hook command through a shell on some platforms, and a path with spaces that is
    not quoted correctly fails to launch, which looks exactly like "the hook never
    fired". Written once here so documentation cannot drift from it.
    """
    python = Path(sys.executable).resolve()
    hooks = {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f'"{python}" "{recorder}" {event}',
                        "timeout": 30,
                    }
                ]
            }
        ]
        for event in HOOK_EVENTS
    }
    settings.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


def _probe(
    workspace: Path, log: Path, version: str, args: argparse.Namespace
) -> list[dict[str, object]]:
    """Run one headless session and collect every payload the recorders saw."""
    env = {**_base_env(), "AER_CLAUDE_PROBE_LOG": str(log)}
    if not args.use_auth:
        env["ANTHROPIC_API_KEY"] = "probe-invalid-key"
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    command = [
        args.claude,
        "-p",
        args.prompt,
        "--settings",
        str(workspace / "settings.json"),
        "--verbose",
    ]
    if args.use_auth:
        # A real turn: the configured default model, and the two tools the probe
        # prompt needs. Without this, print mode refuses tool calls and the run
        # yields no PreToolUse/PostToolUse at all.
        command += ["--allowedTools", "Bash(echo *)", "Read"]
    else:
        command += ["--model", args.model]
    workdir = args.workdir or str(workspace)
    print(f"\n=== probing print mode (use_auth={args.use_auth}) in {workdir} ===")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            cwd=workdir,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"claude -p timed out after {args.timeout}s; partial observations kept")
        completed = None

    if completed is not None:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        for line in tail[-5:]:
            print(f"  cli: {line}")

    observations: list[dict[str, object]] = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payload_raw = record["payload"]
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {"_unparsed": payload_raw[:200]}
            observations.append(
                {
                    "event": record["event"],
                    "payload": payload,
                    "keys": sorted(payload) if isinstance(payload, dict) else [],
                }
            )
    return observations


def _base_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    # Keep the probe from inheriting IDE/MCP wiring it does not need.
    for key in ("CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_IDE_PORT"):
        env.pop(key, None)
    return env


def _report(version: str, observations: list[dict[str, object]]) -> dict[str, object]:
    fired: dict[str, dict[str, object]] = {}
    for obs in observations:
        event = str(obs["event"])
        fired.setdefault(event, {"count": 0, "keys": obs["keys"]})
        fired[event]["count"] = int(fired[event]["count"]) + 1
    matrix = [
        {
            "event": event,
            "fired": event in fired,
            "deliveries": fired.get(event, {}).get("count", 0),
            "payload_keys": fired.get(event, {}).get("keys", []),
        }
        for event in HOOK_EVENTS
    ]
    return {
        "claude_version": version,
        "mode": "print",
        "events": matrix,
        "usable": any(cell["fired"] for cell in matrix),
    }


def _print_report(report: dict[str, object], observations: list[dict[str, object]]) -> None:
    print("\n=== hook coverage (print mode) ===")
    for cell in report["events"]:  # type: ignore[index]
        keys = ", ".join(cell["payload_keys"]) if cell["payload_keys"] else "-"
        state = f"CAPTURED x{cell['deliveries']}" if cell["fired"] else "MISSING"
        print(f"  {cell['event']:20} {state:14} keys: {keys}")
    print(
        "\ninteractive (TUI) mode is not probed: it needs a terminal. "
        "Upgrade coverage cells only from captured payloads."
    )


if __name__ == "__main__":
    raise SystemExit(main())
