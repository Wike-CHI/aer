"""Seed a drill database with one of every substantive AER record.

Why this exists: section 11 of the drill asks for a record-level read-back, but
production currently holds **zero** rows in every domain table (it was created by
``alembic upgrade head`` and has only ever been read since). A drill that compares
``0 == 0`` proves nothing about preservation. So the records are created here,
inside the drill sandbox, and the source -> backup -> restore -> read-back chain is
exercised on data that really exists.

Runs entirely inside the drill sandbox. It is never pointed at production.

Usage: python drill_seed.py <data-dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aer import AER
from aer.experience.candidate import ExperienceCandidate
from aer.experience.provider import CallableDistillationProvider
from aer.runtime.enums import EventType
from aer.verification import HttpStatusVerifier, VerificationContext

#: Marker carried through every seeded record so the read-back can prove it is
#: looking at *these* rows and not at something the restore invented.
DRILL_MARKER = "drill-2026-09-17"


def _candidate(evidence: object) -> ExperienceCandidate:
    """A rule-based provider: no model needed, and the shape is deterministic."""
    del evidence
    return ExperienceCandidate(
        domain="disaster-recovery",
        title="Drill-seeded claim: restore must return the backup's point in time",
        problem=(
            "A restore that keeps writes made after the backup is not a restore. "
            "The drill therefore seeds a marker record and checks it disappears "
            "when the backup is re-applied."
        ),
        symptoms=("synthetic drill data",),
        failed_attempts=("using cp instead of the project restore tool",),
        root_cause="Backup point-in-time semantics are only visible with known records.",
        solution="Restore the backup and re-read the seeded ids field by field.",
        recommended_workflow=("seed", "backup", "restore", "compare"),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    data_dir = Path(argv[1])
    data_dir.mkdir(parents=True, exist_ok=True)

    with AER(
        data_dir,
        distillation_provider=CallableDistillationProvider(_candidate, name="drill-rule-based"),
    ) as runtime:
        run = runtime.start_run(task=f"{DRILL_MARKER}: seed a full record set", task_type="drill")

        run.emit(
            EventType.TOOL_CALL,
            input={"tool": "http_request", "url": "https://example.invalid/health"},
            metadata={"marker": DRILL_MARKER},
        )
        error = run.error(
            ConnectionRefusedError("connection refused by example.invalid"),
            metadata={"marker": DRILL_MARKER, "attempt": 1},
        )
        run.emit(EventType.TOOL_RESULT, output={"ok": False, "retryable": True})
        with run.recovery(reason="retry once the endpoint is reachable", error_id=error.id):
            run.emit(EventType.TOOL_RESULT, output={"ok": True, "attempt": 2})

        run.success()

        verdict = runtime.verify(
            run.run_id,
            HttpStatusVerifier(expected_status=200),
            context=VerificationContext(run_id=run.run_id, payload={"actual_status": 200}),
            metadata={"marker": DRILL_MARKER},
        )
        experience = run.distill(explicit_high_value=True)

        # Read everything back *from storage*, not from the objects above: the point
        # is to describe what is durably recorded, not what the API returned.
        stored_run = runtime.get_run(run.run_id)
        events = runtime.get_events(run.run_id)
        recoveries = runtime.get_recoveries(run.run_id)
        verifications = runtime.get_verifications(run.run_id)
        sources = runtime.get_experience_sources(experience.id) if experience else []

        summary = {
            "marker": DRILL_MARKER,
            "run": {
                "id": stored_run.id,
                "task": stored_run.task_description,
                "task_type": stored_run.task_type,
                "status": stored_run.status.value,
                "started_at": stored_run.started_at.isoformat(),
            },
            "events": {
                "count": len(events),
                "first_id": events[0].id if events else None,
                "types": [event.event_type.value for event in events],
            },
            "error": {
                "id": error.id,
                "error_type": error.error_type,
                "resolved": runtime.errors.get(error.id).resolved,
            },
            "recoveries": {
                "count": len(recoveries),
                "first": {
                    "id": recoveries[0].id,
                    "success": recoveries[0].success,
                    "reason": recoveries[0].reason,
                }
                if recoveries
                else None,
            },
            "verification": {
                "id": verdict.id,
                "verifier_name": verdict.verifier_name,
                "passed": verdict.passed,
                "required": verdict.required,
                "recorded": [
                    {"id": record.id, "passed": record.passed} for record in verifications
                ],
            },
            "experience": {
                "id": experience.id if experience else None,
                "kind": experience.kind.value if experience else None,
                "domain": experience.domain if experience else None,
                "status": experience.status.value if experience else None,
                "title": experience.title if experience else None,
            },
            "experience_sources": {
                "count": len(sources),
                "run_ids": [source.run_id for source in sources],
            },
            "verified_success": runtime.verified_success(run.run_id),
        }

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
