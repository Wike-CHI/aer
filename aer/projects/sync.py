"""Explicit SSH delivery and server-owned acceptance of Claude capture."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from aer import AER, ExperienceCandidate
from aer.config import load_deployment_config
from aer.projects.central import closeout_central, receive, stream_key
from aer.projects.config import Project, load_project
from aer.projects.outbox import MAX_BATCH, atomic, batches, canonical, directory


def push(project: Project, host: str, manifest: str, container: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]*", host):
        raise ValueError("Use a configured SSH host alias")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container) or not manifest.startswith("/"):
        raise ValueError("Invalid container or absolute manifest path")
    command = shlex.join(
        [
            "docker",
            "exec",
            "-i",
            container,
            "python",
            "-m",
            "aer.projects.sync",
            "receive",
            "--manifest",
            manifest,
        ]
    )
    latest: dict[str, Any] = {"status": "empty", "acknowledged": 0}
    for body in batches(project):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
            input=canonical(body),
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"SSH receiver failed (exit {result.returncode}); outbox retained")
        if len(result.stdout) > MAX_BATCH:
            raise ValueError("Oversized receiver acknowledgement")
        ack = json.loads(result.stdout)
        expected = stream_key(project.project_id, project.task_id, project.sync_source_id or "")
        if (
            ack.get("schema_version") != "aer.claude.sync.v1"
            or ack.get("stream_key") != expected
            or type(ack.get("acknowledged")) is not int
            or ack["acknowledged"] < body["items"][-1]["sequence"]
            or not isinstance(ack.get("runs"), dict)
        ):
            raise ValueError("Invalid receiver acknowledgement; outbox retained")
        atomic(directory(project) / "last-ack.json", ack)
        latest = ack
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    send = subs.add_parser("push")
    send.add_argument("--project", type=Path, required=True)
    send.add_argument("--ssh-host", required=True)
    send.add_argument("--manifest", default="/artifacts/central-projects.json")
    send.add_argument("--container", default="aer-aer-mcp-1")
    recv = subs.add_parser("receive")
    recv.add_argument("--manifest", type=Path, required=True)
    finish = subs.add_parser("closeout")
    finish.add_argument("--manifest", type=Path, required=True)
    for name in ("project-id", "task-id", "source-id", "revision"):
        finish.add_argument(f"--{name}", required=True)
    subs.add_parser("project-index")
    args = parser.parse_args(argv)
    output: Any
    if args.command == "push":
        output = push(load_project(args.project), args.ssh_host, args.manifest, args.container)
    else:
        config = load_deployment_config()
        with AER(
            config.db_path.parent,
            db_filename=config.db_path.name,
            knowledge_dir=config.knowledge_dir,
        ) as aer:
            if args.command == "project-index":
                output = {"projection": str(aer.project_experiences())}
            else:
                raw = sys.stdin.buffer.read(MAX_BATCH + 1)
                if len(raw) > MAX_BATCH:
                    raise ValueError("Input exceeds limit")
                if args.command == "receive":
                    output = receive(aer, args.manifest, json.loads(raw))
                else:
                    if len(raw) > 65536:
                        raise ValueError("Candidate exceeds limit")
                    candidate = ExperienceCandidate.model_validate_json(raw)
                    output = closeout_central(
                        aer,
                        args.manifest,
                        args.project_id,
                        args.task_id,
                        args.source_id,
                        args.revision,
                        candidate,
                        db_filename=config.db_path.name,
                    )
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
