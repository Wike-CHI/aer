"""Server-owned central Claude receiver, serialized and auditable, never a DB replacement."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aer import AER
from aer.adapter.claude.adapter import ClaudeCodeAdapter
from aer.adapter.claude.mapping import CLAUDE_PROVIDER, ClaudeHookEvent, event_name
from aer.adapter.protocol import AgentExecutionEnvelope
from aer.projects.config import Check, Project
from aer.projects.outbox import MAX_BATCH, atomic, canonical, digest, exclusive
from aer.runtime.sanitization import sanitize_json
from aer.runtime.serialization import to_json_object


class SourceAdapter(ClaudeCodeAdapter):
    """Keep pre-redaction source identities when payload summaries collapse."""

    def handle_event(self, raw: Mapping[str, object]) -> Sequence[AgentExecutionEnvelope]:
        return [
            event.model_copy(
                update={"external_event_id": f"sync:{raw['_source_event_id']}:{index}"}
            )
            for index, event in enumerate(super().handle_event(raw))
        ]


def assert_complete(aer: AER, run_id: str, metadata: dict[str, Any]) -> None:
    key = metadata.get("stream_key")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid central stream")
    state = json.loads((aer.data_dir / "central-sync" / f"{key}.json").read_bytes())
    local = metadata.get("local_run_id")
    if state["pending"] is not None or local not in state["closed"]:
        raise ValueError("Central source capture incomplete")
    if state["runs"].get(local) != run_id:
        raise ValueError("Central source run mismatch")


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ids: list[str] = Field(min_length=1)
    root: Path
    checks: dict[str, Check] = Field(default_factory=dict)
    customer_id: str | None = None
    site_id: str | None = None


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects: dict[str, Registration]


def registration(path: Path, project_id: str, source_id: str) -> Registration:
    if not path.is_absolute():
        raise ValueError("Registry must be an absolute server-owned path")
    registry = Registry.model_validate_json(path.read_bytes())
    found = registry.projects.get(project_id)
    if found is None or source_id not in found.source_ids:
        raise ValueError("Project/device not registered on receiver")
    return found


def stream_key(project_id: str, task_id: str, source_id: str) -> str:
    for value in (project_id, task_id, source_id):
        Project.stable_id(value)
    return digest([project_id, task_id, source_id])


def receive(aer: AER, registry_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    if len(canonical(body)) > MAX_BATCH or body.get("schema_version") != "aer.claude.sync.v1":
        raise ValueError("Invalid or oversized sync envelope")
    if set(body) != {"schema_version", "project_id", "task_id", "source_id", "items"}:
        raise ValueError("Unexpected sync envelope fields")
    project_id, task_id, source_id = (body[k] for k in ("project_id", "task_id", "source_id"))
    key = stream_key(project_id, task_id, source_id)
    registered = registration(registry_path, project_id, source_id)
    items = body["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Expected 1..100 events")
    folder = aer.data_dir / "central-sync"
    folder.mkdir(exist_ok=True)
    state_path = folder / f"{key}.json"
    # Global lock prevents two task streams from racing the shared runtime.
    with exclusive(folder / ".lock"):
        state = (
            json.loads(state_path.read_bytes())
            if state_path.exists()
            else {
                "project_id": project_id,
                "task_id": task_id,
                "source_id": source_id,
                "receipts": [],
                "pending": None,
                "runs": {},
                "closed": [],
            }
        )
        if state["pending"] is not None:
            raise ValueError(
                "Receiver interrupted with pending evidence; operator reconciliation required"
            )
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "sequence",
                "event_id",
                "payload",
                "local_run_id",
                "observed_at",
                "association",
            }:
                raise ValueError("Malformed outbox item")
            number = item["sequence"]
            if type(number) is not int or number < 1:
                raise ValueError("Invalid event sequence")
            hashed = digest(item)
            if number <= len(state["receipts"]):
                if state["receipts"][number - 1] != hashed:
                    raise ValueError("Conflicting replay")
                continue
            if number != len(state["receipts"]) + 1:
                raise ValueError("Out-of-order event or missing prefix")
            raw = item["payload"]
            association = item["association"]
            if not isinstance(raw, dict) or not isinstance(association, dict):
                raise ValueError("Invalid capture data")
            if (association.get("project_id"), association.get("task_id")) != (project_id, task_id):
                raise ValueError("Cross-project/task association")
            local_run = item["local_run_id"]
            session = raw.get("session_id")
            if not isinstance(local_run, str) or not isinstance(session, str) or not session:
                raise ValueError("Missing source run/session")
            if association.get("tool_session") != session:
                raise ValueError("Source association session mismatch")
            event = event_name(raw)
            remote_session = f"central:{key}:{session}"
            current = aer.find_adapter_session(CLAUDE_PROVIDER, remote_session)
            if current is None and event != ClaudeHookEvent.USER_PROMPT_SUBMIT:
                raise ValueError("Missing initial prompt")
            if current is not None:
                mapped = state["runs"].get(local_run)
                if mapped != current.aer_run_id:
                    raise ValueError("Session belongs to a different source run")
            elif local_run in state["runs"]:
                raise ValueError("Source run belongs to a different session")
            if local_run in state["closed"]:
                raise ValueError("Event after terminal source run")
            if current and aer.adapter_events.count(aer_run_id=current.aer_run_id, applied=False):
                raise ValueError("Unapplied adapter claim; reconciliation required")
            # Claim before applying. This is not advertised as lossless replay.
            state["pending"] = {"sequence": number, "hash": hashed}
            atomic(state_path, state)
            clean = sanitize_json(to_json_object(raw))
            clean["session_id"] = remote_session
            clean["_source_event_id"] = str(item["event_id"])
            meta = sanitize_json(to_json_object(association))
            meta.update(customer_id=registered.customer_id, site_id=registered.site_id)
            adapter = SourceAdapter(detect_version=False)
            handle = aer.adapter_ingestor.open(
                adapter,
                clean,
                task_type="wordpress.build",
                metadata={
                    "building_project": meta,
                    "central_sync": {
                        "stream_key": key,
                        "source_id": source_id,
                        "local_run_id": local_run,
                        "observed_at": item["observed_at"],
                    },
                },
            )
            if local_run in state["runs"] and state["runs"][local_run] != handle.run_id:
                raise ValueError("Source run remapped unexpectedly")
            state["runs"][local_run] = handle.run_id
            if event == ClaudeHookEvent.SESSION_END:
                aer.adapter_ingestor.close(handle, clean)
                state["closed"].append(local_run)
            else:
                aer.adapter_ingestor.ingest(handle, clean)
            if aer.adapter_events.count(aer_run_id=handle.run_id, applied=False):
                raise ValueError("Receiver has unapplied adapter claims")
            state["receipts"].append(hashed)
            state["pending"] = None
            atomic(state_path, state)
        return {
            "schema_version": "aer.claude.sync.v1",
            "stream_key": key,
            "acknowledged": len(state["receipts"]),
            "runs": state["runs"],
        }


def closeout_central(
    aer: AER,
    registry_path: Path,
    project_id: str,
    task_id: str,
    source_id: str,
    revision: str,
    candidate: Any,
    *,
    db_filename: str = "aer.db",
) -> list[Any]:
    from aer.projects.closeout import closeout

    key = stream_key(project_id, task_id, source_id)
    registered = registration(registry_path, project_id, source_id)
    folder = aer.data_dir / "central-sync"
    with exclusive(folder / ".lock"):
        state = json.loads((folder / f"{key}.json").read_bytes())
        if (
            state["pending"] is not None
            or not state["runs"]
            or set(state["closed"]) != set(state["runs"])
        ):
            raise ValueError("Source capture incomplete or not ended")
        project = Project(
            project_id=project_id,
            task_id=task_id,
            root=registered.root,
            data_dir=aer.data_dir,
            stage="server-verification",
            checks=registered.checks,
            customer_id=registered.customer_id,
            site_id=registered.site_id,
        )
        return closeout(
            project,
            list(state["runs"].values()),
            revision,
            candidate,
            storage_path=aer.data_dir,
            knowledge_path=aer.knowledge_dir,
            db_filename=db_filename,
        )
