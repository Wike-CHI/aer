"""Local opt-in, bounded, sanitized outbox. Hook never uses the network."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aer import AER
from aer.adapter.claude.mapping import event_name, external_event_id
from aer.projects.config import Project
from aer.runtime.sanitization import sanitize_json
from aer.runtime.serialization import to_json_object, utc_now

MAX_BATCH = 2 * 1024 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def directory(project: Project) -> Path:
    return project.store / "sync-outbox" / project.task_id


class SyncLockTimeout(ValueError):
    """Bounded lock contention or interrupted owner; no payload in diagnostic."""


@contextmanager
def exclusive(path: Path, *, timeout: float = 0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(utc_now().isoformat())
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SyncLockTimeout("Sync lock timeout; inspect owner before retry") from None
            time.sleep(0.025)
    try:
        yield
    finally:
        path.unlink()


def atomic(path: Path, value: object) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def append(project: Project, payload: Mapping[str, object], run_id: str) -> None:
    folder = directory(project)
    folder.mkdir(parents=True, exist_ok=True)
    # Match adapter identity before sanitization (sanitizing may collapse secrets).
    key = external_event_id(event_name(payload), payload)
    permitted = {
        k: v
        for k, v in payload.items()
        if k
        in {
            "hook_event_name",
            "session_id",
            "prompt_id",
            "prompt",
            "tool_use_id",
            "tool_name",
            "tool_input",
            "tool_response",
            "error",
            "reason",
            "cwd",
            "duration_ms",
            "timestamp",
            "is_interrupt",
        }
    }
    clean = sanitize_json(to_json_object(permitted))
    with exclusive(folder / ".lock"):
        binding = folder / "source.json"
        identity = {"source_id": project.sync_source_id}
        if binding.exists() and json.loads(binding.read_bytes()) != identity:
            raise ValueError("Task outbox belongs to another device identity")
        if not binding.exists():
            atomic(binding, identity)
        files = sorted(folder.glob("[0-9]*.json"))
        for path in files:
            previous = json.loads(path.read_bytes())
            if previous["event_id"] == key:
                if previous["payload"] != clean:
                    raise ValueError("Duplicate hook identity with conflicting result")
                return
        with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
            run = aer.get_run(run_id)
            if run is None:
                raise ValueError("Captured run missing")
            association = run.metadata["building_project"]
        item = {
            "sequence": len(files) + 1,
            "event_id": key,
            "payload": clean,
            "local_run_id": run_id,
            "observed_at": utc_now().isoformat(),
            "association": association,
        }
        if len(canonical(item)) > 131072:
            raise ValueError("Outbox item exceeds limit")
        atomic(folder / f"{len(files) + 1:08d}.json", item)


def batches(project: Project) -> list[dict[str, Any]]:
    if not project.sync_source_id:
        raise ValueError("Project has no registered sync_source_id")
    if project.gap_file.exists() or any(project.pending_dir.glob("*.json")):
        raise ValueError("Capture incomplete: refusing central sync")
    folder = directory(project)
    with exclusive(folder / ".lock"):
        binding = folder / "source.json"
        if binding.exists() and json.loads(binding.read_bytes()) != {
            "source_id": project.sync_source_id
        }:
            raise ValueError("Task outbox belongs to another device identity")
        items = [json.loads(p.read_bytes()) for p in sorted(folder.glob("[0-9]*.json"))]
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        for item in items:
            if aer.adapter_events.count(aer_run_id=item["local_run_id"], applied=False):
                raise ValueError("Unapplied local adapter events")
    result: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []

    def envelope() -> dict[str, Any]:
        return {
            "schema_version": "aer.claude.sync.v1",
            "project_id": project.project_id,
            "task_id": project.task_id,
            "source_id": project.sync_source_id,
            "items": batch.copy(),
        }

    for i, item in enumerate(items, 1):
        if item["sequence"] != i:
            raise ValueError("Outbox sequence gap")
        batch.append(item)
        if len(batch) > 100 or len(canonical(envelope())) > MAX_BATCH:
            batch.pop()
            result.append(envelope())
            batch = [item]
    if batch:
        result.append(envelope())
    return result
