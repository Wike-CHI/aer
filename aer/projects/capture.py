"""Project-scoped Claude capture using its existing measured event translation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from aer import AER
from aer.adapter.claude.adapter import ClaudeCodeAdapter
from aer.adapter.claude.mapping import CLAUDE_PROVIDER, ClaudeHookEvent, event_name
from aer.exceptions import AdapterSessionTerminated
from aer.projects.config import Project, bind_store, code_version


def capture(project: Project, payload: Mapping[str, object]) -> dict[str, object]:
    from aer.projects.outbox import append, exclusive

    # Serialize the whole local delivery, including the ledger and outbox ordering.
    # Bound contention below the supervisor's eight-second hard deadline.
    with exclusive(project.store / ".capture.lock", timeout=4):
        outcome = _capture(project, payload)
        if project.sync_source_id and outcome.get("run_id"):
            append(project, payload, str(outcome["run_id"]))
        return outcome


def _capture(project: Project, payload: Mapping[str, object]) -> dict[str, object]:
    project.check_cwd(payload.get("cwd"))
    original_session = payload.get("session_id")
    if not isinstance(original_session, str) or not original_session.strip():
        raise ValueError("Missing tool session")
    bind_store(project)
    # The explicit project task, not each prompt, is this integration's run unit.
    # Changing task IDs requires a new tool session; see the session binding below.
    binding = project.store / "tool-sessions"
    binding.mkdir(exist_ok=True)
    key = hashlib.sha256(original_session.encode()).hexdigest()
    path = binding / f"{key}.json"
    identity = {"task_id": project.task_id, "session_id": original_session}
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream)
    except FileExistsError:
        if json.loads(path.read_text(encoding="utf-8")) != identity:
            raise ValueError(
                "Tool session already belongs to another task; start a new session"
            ) from None
    raw = dict(payload)
    raw["session_id"] = f"{project.project_id}:{project.task_id}:{original_session}"
    event = event_name(raw)
    adapter = ClaudeCodeAdapter(detect_version=False)
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        existing = aer.find_adapter_session(CLAUDE_PROVIDER, str(raw["session_id"]))
        if existing is None and event != ClaudeHookEvent.USER_PROMPT_SUBMIT:
            return {"status": "skipped", "reason": "awaiting_prompt"}
        metadata = None
        if existing is None:
            metadata = {
                "building_project": {
                    "project_id": project.project_id,
                    "task_id": project.task_id,
                    "stage": project.stage,
                    "customer_id": project.customer_id,
                    "site_id": project.site_id,
                    "tool_session": original_session,
                    "code_version": code_version(project),
                }
            }
        try:
            handle = aer.adapter_ingestor.open(
                adapter,
                raw,
                task_type="wordpress.build",
                metadata=metadata,
            )
        except AdapterSessionTerminated:
            return {"status": "closed", "run_id": existing.aer_run_id if existing else None}
        if event == ClaudeHookEvent.SESSION_END:
            aer.adapter_ingestor.close(handle, raw)
            return {"status": "inconclusive", "run_id": handle.run_id}
        result = aer.adapter_ingestor.ingest(handle, raw)
        gaps = aer.adapter_events.count(aer_run_id=handle.run_id, applied=False)
        return {"status": result.outcome.value, "run_id": handle.run_id, "unapplied_events": gaps}
