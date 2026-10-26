"""Experiment metrics: the TASKS.md #18 indicators, computed from the store.

Everything here is a *read* of runs the harness stamped -- no metric is collected
at execution time. That is deliberate: the store already recorded the trajectory,
so the report can be recomputed, audited, and recomputed differently (TASKS.md #25:
storage is the fact, every reading of it is a projection).

Rate convention: ``None`` -- never ``0.0`` -- when the denominator is empty, the
same rule :class:`~aer.verification.summary.VerificationSummary` uses for
``pass_rate``. A missing number must not render as a bad number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aer.runtime.enums import EventType, RunStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aer.runtime.models import Run
    from aer.runtime.runtime import AER


@dataclass(frozen=True)
class ArmMetrics:
    """The TASKS.md #18 indicators for one arm of one experiment.

    ``None`` means "no denominator", not "bad": ``success_rate`` on an arm with
    zero runs is unknown, and reporting it as ``0.0`` would invent a defeat.
    """

    runs: int
    success_rate: float | None
    verified_success_rate: float | None
    first_pass_success_rate: float | None
    recovery_rate: float | None
    avg_tool_calls: float | None
    avg_duration_seconds: float | None
    retrieval_sessions: int
    experience_hit_rate: float | None
    assisted_success_rate: float | None
    human_intervention_rate: float | None


@dataclass(frozen=True)
class ExperimentReport:
    """Both arms of one experiment, side by side, with the headline comparison."""

    name: str
    control: ArmMetrics
    treatment: ArmMetrics

    @property
    def success_lift(self) -> float | None:
        """Treatment minus control success rate -- the MVP hypothesis of #19.

        ``None`` when either arm has no runs: a lift computed against an empty
        arm is not evidence for or against anything.
        """
        if self.control.success_rate is None or self.treatment.success_rate is None:
            return None
        return self.treatment.success_rate - self.control.success_rate


def _rate(numerator: float, denominator: int) -> float | None:
    """``numerator / denominator``, or ``None`` when there is nothing to average."""
    if denominator == 0:
        return None
    return numerator / denominator


def _arm_metrics(runtime: AER, runs: list[Run]) -> ArmMetrics:
    """Fold one arm's runs into its indicators."""
    succeeded = 0
    verified = 0
    first_pass = 0
    recovered = 0
    intervened = 0
    tool_call_total = 0
    duration_total = 0.0
    finished = 0
    sessions_total = 0
    runs_with_hit = 0
    hits_succeeded = 0

    for run in runs:
        events = runtime.get_events(run.id)
        tool_calls = sum(1 for event in events if event.event_type is EventType.TOOL_CALL)
        had_error = any(event.event_type is EventType.ERROR for event in events)
        had_recovery = any(event.event_type is EventType.RECOVERY_START for event in events)
        had_human = any(event.event_type is EventType.HUMAN_FEEDBACK for event in events)

        tool_call_total += tool_calls
        succeeded += run.status is RunStatus.SUCCESS
        verified += runtime.verified_success(run.id)
        first_pass += run.status is RunStatus.SUCCESS and not had_error and not had_recovery
        recovered += had_recovery
        intervened += had_human
        if run.ended_at is not None:
            duration_total += (run.ended_at - run.started_at).total_seconds()
            finished += 1

        # The control arm is expected to have none of these; counting what is
        # there (instead of assuming) keeps a holdout breach visible.
        sessions = runtime.retrieval_sessions.list(run_id=run.id, limit=10_000)
        hits = sum(1 for session in sessions if session.result_count > 0)
        sessions_total += len(sessions)
        if hits:
            runs_with_hit += 1
            hits_succeeded += run.status is RunStatus.SUCCESS

    count = len(runs)
    return ArmMetrics(
        runs=count,
        success_rate=_rate(succeeded, count),
        verified_success_rate=_rate(verified, count),
        first_pass_success_rate=_rate(first_pass, count),
        recovery_rate=_rate(recovered, count),
        avg_tool_calls=_rate(tool_call_total, count),
        avg_duration_seconds=_rate(duration_total, finished),
        retrieval_sessions=sessions_total,
        experience_hit_rate=_rate(runs_with_hit, count),
        assisted_success_rate=_rate(hits_succeeded, runs_with_hit),
        human_intervention_rate=_rate(intervened, count),
    )


def compute_report(runtime: AER, *, name: str) -> ExperimentReport:
    """Compute both arms' metrics for the experiment called ``name``.

    Runs are found by their stamped metadata, so the report works on any store the
    experiment wrote to -- recomputing after the fact needs no sidecar file.
    Assignment groups come from the stamp itself, not from re-deriving
    :meth:`~aer.experiment.harness.Experiment.arm_for`: the stamp is what
    actually happened, the hash is only how it was decided.
    """
    control: list[Run] = []
    treatment: list[Run] = []
    offset = 0
    while True:
        batch = runtime.list_runs(limit=500, offset=offset, newest_first=False)
        if not batch:
            break
        offset += len(batch)
        for run in batch:
            experiment = run.metadata.get("experiment")
            if not isinstance(experiment, dict) or experiment.get("name") != name:
                continue
            if experiment.get("assignment") == "treatment":
                treatment.append(run)
            elif experiment.get("assignment") == "control":
                control.append(run)
    return ExperimentReport(
        name=name,
        control=_arm_metrics(runtime, control),
        treatment=_arm_metrics(runtime, treatment),
    )
