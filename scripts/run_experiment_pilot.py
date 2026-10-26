"""Pilot run of the A/B experiment harness (TASKS.md #17-#19).

This script validates the *harness* end to end: seeded assignment, arm-blind
execution, stamped provenance, and the #18 metrics computed from the store. It
does **not** test the MVP hypothesis -- the executor here is scripted, so the
outcome difference is built in by hand. The hypothesis test is reserved for the
real WordPress agent, 50+ tasks per arm (TASKS.md #17); until then, a lift number
from this pilot means "the machinery reports a difference", never "experience
helps".

Usage::

    python scripts/run_experiment_pilot.py [--data-dir DIR] [--tasks N] [--seed S]

The knowledge index is a scripted in-memory double: real distilled experiences,
deterministic keyword-free search (every stored experience whose kind matches the
query's class is returned). Keyword relevance is the engine's job
(``tests/knowledge``), not the pilot's.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aer import (  # noqa: E402
    AER,
    CallableDistillationProvider,
    Experience,
    Experiment,
    HttpStatusVerifier,
    VerificationContext,
    compute_report,
)
from aer.experiment.metrics import ArmMetrics, ExperimentReport  # noqa: E402
from aer.knowledge.base import (  # noqa: E402
    IndexedExperience,
    IndexedRunRef,
    IndexMatch,
    IndexQuery,
)

DOMAIN = "pilot-wp"
EXPERIMENT_NAME = "pilot"


# ---------------------------------------------------------------------------
# scripted knowledge index: real experiences, class-filtered search
# ---------------------------------------------------------------------------


class PilotIndex:
    """A :class:`~aer.knowledge.base.KnowledgeIndex` over projected experiences.

    Search honours the query's class filters (kinds / statuses / outcome_verified)
    so the retriever's per-class searches never see the same experience twice --
    the duplicate-usage constraint that exists in production applies here too.
    Relevance ranking is deliberately absent: every matching experience is a hit
    with a fixed score, because *ranking* is what the pilot must not be measuring.
    """

    def __init__(self) -> None:
        self._experiences: dict[str, IndexedExperience] = {}

    # -- protocol ----------------------------------------------------------

    def ensure_schema(self) -> None:
        pass

    def projection_version(self) -> int | None:
        return 1

    def upsert_experiences(self, experiences, run_refs) -> None:  # type: ignore[no-untyped-def]
        for experience in experiences:
            self._experiences[experience.id] = experience

    def delete_experiences(self, experience_ids) -> None:  # type: ignore[no-untyped-def]
        for experience_id in experience_ids:
            self._experiences.pop(experience_id, None)

    def search(self, query: IndexQuery) -> list[IndexMatch]:
        matches = []
        for experience in self._experiences.values():
            if query.filters.kinds is not None and experience.kind not in query.filters.kinds:
                continue
            if (
                query.filters.statuses is not None
                and experience.status not in query.filters.statuses
            ):
                continue
            if (
                query.filters.outcome_verified is not None
                and experience.outcome_verified is not query.filters.outcome_verified
            ):
                continue
            matches.append(
                IndexMatch(
                    experience_id=experience.id,
                    kind=experience.kind,
                    status=experience.status,
                    domain=experience.domain,
                    title=experience.title,
                    problem=experience.problem,
                    root_cause=experience.root_cause,
                    solution=experience.solution,
                    failed_attempts_text=experience.failed_attempts_text,
                    avoid_text=experience.avoid_text,
                    outcome_verified=experience.outcome_verified,
                    generalizable=experience.generalizable,
                    created_at=experience.created_at,
                    updated_at=experience.updated_at,
                    bm25_score=-1.0,
                )
            )
        return matches[: query.limit]

    def source_counts(self, experience_ids) -> dict[str, int]:  # type: ignore[no-untyped-def]
        return {
            experience_id: len(self._experiences[experience_id].run_ids)
            for experience_id in experience_ids
            if experience_id in self._experiences
        }

    def fingerprints(self) -> dict[str, str]:
        return {
            experience_id: experience.updated_at
            for experience_id, experience in self._experiences.items()
        }

    def count_experiences(self, *, include_deprecated: bool = True) -> int:
        return len(self._experiences)

    def reset(self) -> None:
        self._experiences.clear()

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# scripted agent: the same code in both arms, branching only on what retrieval
# returned -- which is exactly the causal difference under measurement
# ---------------------------------------------------------------------------


def execute_task(task: object) -> None:
    """One page-update attempt. With experience: first pass. Without: 403, repair,
    retry -- the recovery trajectory the learning phase distilled."""
    guidance = task.retrieve(  # type: ignore[attr-defined]
        "how do I avoid the 403 when updating the page", domain=DOMAIN
    )
    attempt = task.run.tool("wp.update_page", input={"page_id": 123})  # type: ignore[attr-defined]
    try:
        with attempt:
            if guidance is None:
                raise PermissionError("403 Forbidden")
            attempt.set_result({"status": 200, "h1": "New H1"})
    except PermissionError:
        error = attempt.error_record
        assert error is not None
        with task.run.recovery(reason="grant edit_posts", error_id=error.id) as recovery:  # type: ignore[attr-defined]
            recovery.set_result({"action": "granted"})
        with task.run.tool("wp.update_page", input={"page_id": 123}) as tool:  # type: ignore[attr-defined]
            tool.set_result({"status": 200, "h1": "New H1"})
    task.run.success()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def learning_runs(aer: AER) -> None:
    """Two recovery trajectories outside the experiment, distilled into the store
    the treatment arm will later retrieve from."""
    for index in range(2):
        run = aer.start_run(task=f"learn: fix page {index}", task_type=DOMAIN)
        attempt = run.tool("wp.update_page", input={"page_id": index})
        try:
            with attempt:
                raise PermissionError("403 Forbidden")
        except PermissionError:
            pass
        error = attempt.error_record
        assert error is not None
        with run.recovery(reason="grant edit_posts", error_id=error.id) as recovery:
            recovery.set_result({"action": "granted"})
        with run.tool("wp.update_page", input={"page_id": index}) as tool:
            tool.set_result({"status": 200})
        run.success()
        # Verify the repair so the distilled experience is VERIFIED -- the
        # retrieval policy serves verified records as guidance; an unverified
        # DISTILLED record would be invisible to the arm we are trying to feed.
        run.verify(
            HttpStatusVerifier(expected_status=200),
            context=VerificationContext(run_id=run.run_id, payload={"actual_status": 200}),
        )
        aer.distill_run(run.run_id)


def project_experiences(aer: AER, index: PilotIndex) -> int:
    """Push every stored experience into the scripted index, as the projector would."""
    experiences = aer.list_experiences(include_deprecated=True, limit=10_000)
    indexed = tuple(_indexed(aer, experience) for experience in experiences)
    run_ids = {
        run_id
        for experience in experiences
        for run_id in aer.experience_sources.get_runs(experience.id)
    }
    refs = tuple(
        IndexedRunRef(
            id=run_id,
            status=(aer.get_run(run_id).status.value if aer.get_run(run_id) else ""),
            agent_name="pilot-script",
            agent_version="1",
        )
        for run_id in sorted(run_ids)
    )
    index.upsert_experiences(indexed, refs)
    return len(indexed)


def _indexed(aer: AER, experience: Experience) -> IndexedExperience:
    return IndexedExperience(
        id=experience.id,
        kind=experience.kind.value,
        status=experience.status.value,
        domain=experience.domain,
        title=experience.title,
        problem=experience.problem,
        root_cause=experience.root_cause or "",
        solution=experience.solution or "",
        failed_attempts_text="\n".join(experience.failed_attempts),
        avoid_text="\n".join(experience.avoid),
        outcome_verified=experience.outcome_verified,
        generalizable=experience.generalizable,
        created_at=experience.created_at.isoformat(),
        updated_at=experience.updated_at.isoformat(),
        run_ids=tuple(aer.experience_sources.get_runs(experience.id)),
    )


def provider() -> CallableDistillationProvider:
    """A rule-based distiller: enough prose to make the retrieval hit meaningful."""

    def distill(evidence: object):  # type: ignore[no-untyped-def]
        from aer import ExperienceCandidate, ExperienceKind

        return ExperienceCandidate(
            domain=DOMAIN,
            title="WordPress REST API 403 on page update",
            problem="Updating a page returns 403 and the content never changes",
            kind=ExperienceKind.RECOVERY,
            root_cause="the credential lacks the edit_posts capability",
            solution="grant edit_posts to the credential, then retry the update",
            failed_attempts=("retrying the same request",),
            avoid=("retrying without checking the capability",),
        )

    return CallableDistillationProvider(distill, name="pilot-rule-based")


def run_pilot(data_dir: Path, *, tasks: int, seed: int) -> ExperimentReport:
    index = PilotIndex()
    with AER(data_dir, distillation_provider=provider(), knowledge_index=index) as aer:
        learning_runs(aer)
        projected = project_experiences(aer, index)
        if projected == 0:
            raise RuntimeError("pilot learned nothing; check the learning phase")

        experiment = Experiment(aer, name=EXPERIMENT_NAME, seed=seed)
        for i in range(tasks):
            task = experiment.begin_task(f"update page {i}", unit=f"task-{i}", task_type=DOMAIN)
            execute_task(task)
        return compute_report(aer, name=EXPERIMENT_NAME)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def render_report(report: ExperimentReport) -> str:
    lines = [
        f"experiment  : {report.name}",
        "",
        f"{'metric':<28}{'control':>10}{'treatment':>10}",
    ]
    rows: list[tuple[str, ArmMetrics, ArmMetrics]] = [
        ("runs", lambda m: str(m.runs)),
        ("success rate", lambda m: _rate(m.success_rate)),
        ("verified success rate", lambda m: _rate(m.verified_success_rate)),
        ("first pass success", lambda m: _rate(m.first_pass_success_rate)),
        ("recovery rate", lambda m: _rate(m.recovery_rate)),
        ("avg tool calls", lambda m: _rate(m.avg_tool_calls)),
        ("avg duration s", lambda m: _rate(m.avg_duration_seconds)),
        ("retrieval sessions", lambda m: str(m.retrieval_sessions)),
        ("experience hit rate", lambda m: _rate(m.experience_hit_rate)),
        ("assisted success rate", lambda m: _rate(m.assisted_success_rate)),
        ("human intervention", lambda m: _rate(m.human_intervention_rate)),
    ]
    for label, pick in rows:
        lines.append(f"{label:<28}{pick(report.control):>10}{pick(report.treatment):>10}")
    lines.append("")
    lift = report.success_lift
    lines.append("success lift (treatment - control): " + ("-" if lift is None else f"{lift:+.2f}"))
    lines.append("")
    lines.append(
        "NOTE: the executor is scripted; this run validates the harness and the "
        "metrics, not the MVP hypothesis (TASKS.md #17 reserves the real test for "
        "the real WordPress agent, 50+ tasks per arm)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data/pilot", help="pilot data directory")
    parser.add_argument("--tasks", type=int, default=20, help="tasks per experiment")
    parser.add_argument("--seed", type=int, default=7, help="assignment seed")
    args = parser.parse_args(argv)

    report = run_pilot(Path(args.data_dir), tasks=args.tasks, seed=args.seed)
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
