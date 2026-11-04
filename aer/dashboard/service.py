"""Bounded read models over the existing runtime; no SQL or state transitions."""

from __future__ import annotations

from collections import Counter

from aer import AER
from aer.runtime.enums import ExperienceStatus, RunStatus


def overview(aer: AER) -> dict[str, object]:
    recent = aer.list_runs(limit=100)
    summaries = aer.verifications.summaries_by_run([run.id for run in recent])
    days = Counter(run.started_at.date().isoformat() for run in recent)
    return {
        "runs": aer.runs.count(),
        "experiences": aer.experiences.count(),
        "retrieval_sessions": aer.retrieval_sessions.count(),
        "retrieved_items": aer.experience_usage.count(),
        "injected_items": aer.experience_usage.count(injected_only=True),
        "run_states": {status.value: aer.runs.count(status=status) for status in RunStatus},
        "experience_states": {
            status.value: aer.experiences.count(status=status) for status in ExperienceStatus
        },
        "recent_sample_size": len(recent),
        "recent_verified": sum(aer.verified_success(run.id) for run in recent),
        "recent_without_required_checks": sum(
            run.id not in summaries or summaries[run.id].required_total == 0 for run in recent
        ),
        "recent_daily": dict(sorted(days.items())),
        "notice": (
            "验证与声明独立。趋势和验证指标仅统计最近 100 次任务。"
            "纯检索不记录使用。采用未知不能视为未采用。"
        ),
    }


def runs(aer: AER, offset: int, limit: int, status: str | None) -> dict[str, object]:
    selected = RunStatus(status) if status else None
    items = aer.list_runs(status=selected, offset=offset, limit=limit)
    return {
        "total": aer.runs.count(status=selected),
        "items": [
            {
                "id": run.id,
                "task": run.task_description[:500],
                "status": run.status.value,
                "started_at": run.started_at.isoformat(),
                "verified_success": aer.verified_success(run.id),
                "verification": aer.get_verification_summary(run.id).model_dump(mode="json"),
            }
            for run in items
        ],
    }


def run_detail(aer: AER, identifier: str, offset: int, limit: int) -> dict[str, object] | None:
    run = aer.get_run(identifier)
    if run is None:
        return None
    return {
        "run": run.model_dump(mode="json"),
        "verified_success": aer.verified_success(identifier),
        "verification_summary": aer.get_verification_summary(identifier).model_dump(mode="json"),
        "verifications": [
            v.model_dump(mode="json") for v in aer.get_verifications(identifier, limit=100)
        ],
        "events": [
            v.model_dump(mode="json")
            for v in aer.get_events(identifier, offset=offset, limit=limit)
        ],
        "event_total": aer.events.count_by_run(identifier),
        "errors": [v.model_dump(mode="json") for v in aer.get_errors(identifier, limit=100)],
        "recoveries": [
            v.model_dump(mode="json") for v in aer.get_recoveries(identifier, limit=100)
        ],
        "detail_limit": 100,
    }


def experiences(aer: AER, offset: int, limit: int, status: str | None) -> dict[str, object]:
    selected = ExperienceStatus(status) if status else None
    return {
        "items": [
            item.model_dump(mode="json")
            for item in aer.list_experiences(
                status=selected, include_deprecated=True, offset=offset, limit=limit
            )
        ],
        "total": aer.experiences.count(status=selected),
    }


def experience_detail(aer: AER, identifier: str) -> dict[str, object] | None:
    item = aer.get_experience(identifier)
    if item is None:
        return None
    return {
        "experience": item.model_dump(mode="json"),
        "source_run_ids": aer.experience_sources.get_runs(identifier),
        "usage": [
            v.model_dump(mode="json") for v in aer.list_experience_usage(identifier, limit=100)
        ],
        "usage_limit": 100,
    }
