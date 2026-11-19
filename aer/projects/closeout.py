"""Owner-operated evidence collection, separate from lightweight hook capture."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from aer import AER, CallableDistillationProvider, ExperienceCandidate
from aer.projects.config import Check, Project, bind_store, code_version
from aer.runtime.enums import RunStatus
from aer.runtime.run import RunContext
from aer.runtime.sanitization import redact
from aer.runtime.serialization import JsonObject, utc_now
from aer.verification.base import VerificationContext, VerificationResult
from aer.verification.environment import CallableEnvironmentVerifier


def closeout(
    project: Project, run_ids: list[str], revision: str, candidate: ExperienceCandidate
) -> list[JsonObject]:
    """Run owner-configured argv without shell interpolation or a client pass flag.

    Commit the task changes before closeout. A named check's scope says what its
    exit status actually establishes. Syntax/field-only checks do not verify the
    complete task. Failed checks are retained; start a new repair run to retry.
    """
    bind_store(project)
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise ValueError("Select distinct source runs")
    if project.gap_file.exists() or any(project.pending_dir.glob("*.json")):
        raise ValueError("Task has capture gaps; inspect diagnostics and recapture a new task")
    version = code_version(project, timeout=5)
    if version["commit"] != revision or version["dirty"] is not False:
        raise ValueError("Closeout requires the selected clean committed revision")
    if not project.checks:
        raise ValueError("No owner-controlled checks configured")
    provider = CallableDistillationProvider(name="project-candidate", func=lambda _: candidate)
    results: list[JsonObject] = []
    with AER(
        project.store, knowledge_dir=project.knowledge_dir, distillation_provider=provider
    ) as aer:
        # Validate every association before performing any check or mutation.
        contexts = []
        for run_id in run_ids:
            run = aer.get_run(run_id)
            if run is None:
                raise ValueError("Unknown source run")
            binding = run.metadata.get("building_project")
            if not isinstance(binding, dict) or (
                binding.get("project_id"),
                binding.get("task_id"),
            ) != (project.project_id, project.task_id):
                raise ValueError("Source run belongs to another project or task")
            if aer.adapter_events.count(aer_run_id=run_id, applied=False):
                raise ValueError("Source run has unapplied event claims")
            if aer.get_verification_summary(run_id).total:
                raise ValueError("Run already checked; preserve history and use a new repair run")
            contexts.append(RunContext(aer, run))
        task_covered = any(check.scope == "task" for check in project.checks.values())
        for ctx in contexts:
            if not ctx.is_finished:
                ctx.finish(RunStatus.INCONCLUSIVE)

            def verify_suite(vc: VerificationContext) -> VerificationResult:
                checks = [
                    execute_check(project, spec, name, vc.run_id, revision)
                    for name, spec in project.checks.items()
                ]
                return VerificationResult(
                    passed=all(check.passed for check in checks),
                    result={"checks": [check.model_dump(mode="json") for check in checks]},
                    metadata={"source": "owner-check-suite", "task_covered": task_covered},
                )

            # Commit exactly one verdict after the complete suite returns. A passing
            # prefix followed by an exception must never constitute required PASS.
            verifier = CallableEnvironmentVerifier(
                name="project:closeout-suite", required=task_covered, check=verify_suite
            )
            aer.verify(
                ctx.run_id,
                verifier,
                context=VerificationContext(
                    run_id=ctx.run_id,
                    metadata={
                        "project_id": project.project_id,
                        "task_id": project.task_id,
                        "revision": revision,
                    },
                ),
            )
            # SDK decides kind, eligibility, trust and dedup; never assign VERIFIED.
            experience = aer.distill_run(ctx.run_id, explicit_high_value=True)
            results.append(
                {
                    "run_id": ctx.run_id,
                    "verified_success": aer.verified_success(ctx.run_id),
                    "verification": aer.get_verification_summary(ctx.run_id).model_dump(
                        mode="json"
                    ),
                    "experience": None
                    if experience is None
                    else experience.model_dump(mode="json"),
                }
            )
    return results


def execute_check(
    project: Project, spec: Check, name: str, run_id: str, revision: str
) -> VerificationResult:
    before = code_version(project, timeout=5)
    if before["commit"] != revision or before["dirty"] is not False:
        raise ValueError("Code changed before verification")
    completed = subprocess.run(
        spec.argv,
        cwd=project.root,
        capture_output=True,
        timeout=spec.timeout,
        check=False,
    )
    after = code_version(project, timeout=5)
    if after != before:
        raise ValueError("Code changed during verification; result cannot attest selected revision")
    log = redact((completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace"))
    if len(log) > 65536:
        log = log[:65536] + "\n[truncated diagnostic; exit code is independently observed]"
    artifact_dir = project.store / "evidence" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(log.encode()).hexdigest()
    artifact = artifact_dir / f"{digest}.log"
    artifact.write_text(log, encoding="utf-8")
    return VerificationResult(
        passed=completed.returncode == 0,
        result={
            "exit_code": completed.returncode,
            "scope": spec.scope,
            "description": spec.description,
            "revision": revision,
            "evidence_ref": str(artifact),
            "sha256": digest,
            "checked_at": utc_now().isoformat(),
        },
        metadata={"source": "owner-configured-command", "check_name": name},
    )


def load_candidate(path: Path) -> ExperienceCandidate:
    return ExperienceCandidate.model_validate(json.loads(path.read_text(encoding="utf-8")))
