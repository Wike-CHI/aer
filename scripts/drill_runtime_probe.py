"""Probe a drill database through the *current* runtime.

This answers the question a cross-revision recovery actually asks: once an old store
has been migrated forward, can this image read the history it holds -- and can it
keep working?

Two properties are deliberate:

* **It will not migrate.** ``AER(...)`` applies pending migrations on construction,
  so opening a store that is not at head *is* a migration. The probe checks the
  revision first and refuses to open unless ``--migrate`` is passed. A cross-revision
  drill found the smoke test doing this silently; a tool that inspects a recovered
  database must not be able to change it by accident.
* **The write is opt-in.** Reads always happen; ``--write`` additionally creates a
  run, verifies it and distils it. A probe that writes by default is a probe that
  will eventually write somewhere it should not.

Reads and the write go through the public API on purpose. "The old rows parse" and
"the engine still works against this store" are different claims, and only the
second one touches the tables that 0004 added.

Usage::

    python drill_runtime_probe.py <data-dir> [--write] [--migrate] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aer import AER, __version__
from aer.experience.candidate import ExperienceCandidate
from aer.experience.provider import CallableDistillationProvider
from aer.runtime.enums import EventType
from aer.storage.migrations import current_revision, head_revision
from aer.verification import HttpStatusVerifier, VerificationContext

#: The historical runs the cross-revision fixture seeds. Named rather than
#: discovered so the probe states what it expects, and a missing row is a finding
#: instead of an empty loop.
EXPECTED_RUNS = ("run-0003-verified", "run-0003-recovered")


def _candidate(evidence: object) -> ExperienceCandidate:
    del evidence
    return ExperienceCandidate(
        domain="cross-revision",
        title="A store migrated forward still works",
        problem="An old schema must reach head without losing history or capability.",
        symptoms=("none",),
        solution="Migrate, then read the old rows and write new ones.",
        recommended_workflow=("restore", "upgrade", "read", "write"),
    )


def probe(data_dir: Path, *, write: bool, allow_migrate: bool) -> dict[str, object]:
    database = data_dir / "aer.db"
    report: dict[str, object] = {
        "aer_version": __version__,
        "data_dir": data_dir.as_posix(),
        "db_path": database.as_posix(),
        "revision_before_open": current_revision(database),
        "head": head_revision(),
    }

    if report["revision_before_open"] != report["head"] and not allow_migrate:
        report["opened"] = False
        report["reason"] = (
            "the store is not at head, and opening it through AER(...) would migrate "
            "it; pass --migrate if that is intended"
        )
        return report

    provider = CallableDistillationProvider(_candidate, name="drill-cross-revision")
    with AER(data_dir, distillation_provider=provider) as runtime:
        report["opened"] = True
        report["revision_after_open"] = runtime.database.schema_revision()
        report["reads"] = _reads(runtime)
        if write:
            report["write"] = _write(runtime)
        report["counts_after"] = {
            "runs": runtime.runs.count(),
            "experiences": runtime.experiences.count(),
        }
    return report


def _reads(runtime: AER) -> dict[str, object]:
    """Read the historical records back through the public API."""
    reads: dict[str, object] = {"runs": runtime.runs.count()}
    for run_id in EXPECTED_RUNS:
        run = runtime.get_run(run_id)
        reads[run_id] = (
            None
            if run is None
            else {
                "status": run.status.value,
                "task": run.task_description,
                "events": runtime.events.count_by_run(run_id),
                "errors": runtime.errors.count_by_run(run_id),
                "recoveries": runtime.recoveries.count_by_run(run_id),
                "verifications": runtime.verifications.count_by_run(run_id),
                "verified_success": runtime.verified_success(run_id),
            }
        )
    error = runtime.errors.get("err-0003-1")
    recovery = runtime.recoveries.get("rec-0003-1")
    verdict = runtime.verifications.get("ver-0003-1")
    reads["error_resolved"] = None if error is None else error.resolved
    reads["recovery_success"] = None if recovery is None else recovery.success
    reads["recovery_reason"] = None if recovery is None else recovery.reason
    reads["verification_passed"] = None if verdict is None else verdict.passed
    reads["verification_name"] = None if verdict is None else verdict.verifier_name
    return reads


def _write(runtime: AER) -> dict[str, object]:
    """One current-version write, end to end: run, verification, experience."""
    run = runtime.start_run(task="post-migration write", task_type="drill")
    run.emit(EventType.TOOL_CALL, input={"tool": "http_request", "url": "https://example.invalid/"})
    run.success()
    verdict = runtime.verify(
        run.run_id,
        HttpStatusVerifier(expected_status=200),
        context=VerificationContext(run_id=run.run_id, payload={"actual_status": 200}),
    )
    experience = run.distill(explicit_high_value=True)
    return {
        "run_id": run.run_id,
        "events": runtime.events.count_by_run(run.run_id),
        "verification_passed": verdict.passed,
        "experience_id": None if experience is None else experience.id,
        "experience_kind": None if experience is None else experience.kind.value,
        "experience_status": None if experience is None else experience.status.value,
        "experience_sources": (
            0
            if experience is None
            else runtime.experience_sources.count_for_experience(experience.id)
        ),
        "verified_success": runtime.verified_success(run.run_id),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drill_runtime_probe.py", description=__doc__)
    parser.add_argument("data_dir", type=Path, help="directory holding aer.db")
    parser.add_argument("--write", action="store_true", help="also perform one write path")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="allow opening a store that is not at head (which will migrate it)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    report = probe(args.data_dir, write=args.write, allow_migrate=args.migrate)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"[probe] opened={report['opened']} revision={report.get('revision_before_open')}")
        if not report["opened"]:
            print(f"[probe] {report['reason']}", file=sys.stderr)
            return 1
        print(f"[probe] reads={report['reads']}")
        if "write" in report:
            print(f"[probe] write={report['write']}")
    return 0 if report["opened"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
