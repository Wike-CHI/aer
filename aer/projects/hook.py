"""Bounded process supervisor: no stdout decisions, models or indexing in hooks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from aer.projects.config import OutOfProjectScope, load_project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    project = None
    pending = None
    worker_exit_code = None
    try:
        project = load_project(args.project)
        # Claude pipes UTF-8 even when Windows' locale is CP936. Reading text stdin
        # with that locale silently corrupts non-ASCII cwd before the scope check.
        raw = (
            sys.stdin.buffer.read(262145).decode("utf-8")
            if hasattr(sys.stdin, "buffer")
            else sys.stdin.read(65537)
        )
        if len(raw) > 65536:
            raise ValueError("Hook payload exceeds 64 KiB")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Hook payload must be an object")
        project.check_cwd(payload.get("cwd"))
        if args.worker:
            from aer.projects.capture import capture

            outcome = capture(project, payload)
            print(json.dumps(outcome), file=sys.stderr)
            return 0
        # A tool may cancel this whole process without allowing our exception
        # handler to run. A unique durable marker makes that loss observable.
        project.pending_dir.mkdir(parents=True, exist_ok=True)
        pending = project.pending_dir / f"{uuid.uuid4().hex}.json"
        pending.write_text(
            json.dumps({"task_id": project.task_id, "state": "pending"}), encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "aer.projects.hook",
                "--project",
                str(args.project),
                "--worker",
            ],
            input=raw,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=8,
            check=False,
        )
        worker_exit_code = result.returncode
        # Do not relay arbitrary process stderr: a startup traceback can contain
        # secrets. Preserve a numeric exit code; the worker reports safe categories.
        if result.returncode:
            raise RuntimeError("Capture worker failed")
        if project.gap_file.exists():
            print("[aer-project] task has a capture gap", file=sys.stderr)
        pending.unlink()
        return 0
    except OutOfProjectScope:
        # Parent project hooks may be inherited in a distinct child checkout.
        # An explicit scope rejection must not poison the parent's task.
        print("[aer-project] skipped: outside registered project", file=sys.stderr)
        return 0
    except Exception as exc:
        # Fail open without pretending capture succeeded. Keep a durable task gap
        # marker when possible; closeout refuses a task with this marker.
        if project is not None:
            try:
                project.gap_file.parent.mkdir(parents=True, exist_ok=True)
                if not project.gap_file.exists():
                    project.gap_file.write_text(
                        json.dumps(
                            {
                                "reason": type(exc).__name__,
                                "task_id": project.task_id,
                                "worker_exit_code": worker_exit_code,
                            }
                        ),
                        encoding="utf-8",
                    )
            except OSError:
                pass
        print(f"[aer-project] capture failed: {type(exc).__name__}", file=sys.stderr)
        return 1 if args.worker else 0


if __name__ == "__main__":
    raise SystemExit(main())
