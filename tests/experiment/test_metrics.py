"""Metrics for the A/B experiment (TASKS #18), computed from the stamped store.

The metrics module is a pure reader: these tests stamp a known trajectory through
the harness, then assert every #18 indicator against hand-computable values. The
convention under test as much as the arithmetic: a rate with an empty denominator
is ``None``, never ``0.0``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import AER, Experiment, compute_report
from tests.experience.support import add_human_feedback
from tests.knowledge.support import RecordingIndex
from tests.usage.support import indexed_for, script_retrieval


def open_aer(data_dir: Path, index: RecordingIndex | None = None) -> AER:
    return AER(data_dir, knowledge_index=index or RecordingIndex())


def give_index_a_hit(aer: AER, index: RecordingIndex) -> None:
    """Script one hit that points at a real stored experience (see usage tests)."""
    script_retrieval(index, guidance=[indexed_for(aer)])


def treatment_unit(experiment: Experiment, start: int = 0) -> str:
    return next(
        f"task-{i}"
        for i in range(start, start + 100)
        if experiment.arm_for(f"task-{i}") == "treatment"
    )


def control_unit(experiment: Experiment, start: int = 0) -> str:
    return next(
        f"task-{i}"
        for i in range(start, start + 100)
        if experiment.arm_for(f"task-{i}") == "control"
    )


def succeed_first_pass(task) -> None:  # type: ignore[no-untyped-def]
    """The clean trajectory: one tool call, no errors, declared success."""
    with task.run.tool("wp.update_page") as tool:
        tool.set_result({"status": 200})
    task.run.success()


def succeed_after_recovery(task) -> None:  # type: ignore[no-untyped-def]
    """Fail once, repair, retry, succeed -- the RECOVERY trajectory."""
    attempt = task.run.tool("wp.update_page")
    try:
        with attempt:
            raise PermissionError("403 Forbidden")
    except PermissionError:
        pass
    error = attempt.error_record
    assert error is not None
    with task.run.recovery(reason="grant edit_posts", error_id=error.id) as recovery:
        recovery.set_result({"action": "granted"})
    with task.run.tool("wp.update_page") as tool:
        tool.set_result({"status": 200})
    task.run.success()


def fail_openly(task) -> None:  # type: ignore[no-untyped-def]
    attempt = task.run.tool("wp.update_page")
    try:
        with attempt:
            raise PermissionError("403 Forbidden")
    except PermissionError:
        pass
    task.run.fail()


class TestGrouping:
    def test_runs_are_grouped_by_their_stamp(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            succeed_first_pass(experiment.begin_task("a", unit=control_unit(experiment)))
            succeed_first_pass(experiment.begin_task("b", unit=treatment_unit(experiment)))
            # Same arms, different experiment: must not leak into the report.
            other = Experiment(aer, name="other", seed=7)
            succeed_first_pass(other.begin_task("c", unit=control_unit(other)))

            report = compute_report(aer, name="pilot")

            assert report.control.runs == 1
            assert report.treatment.runs == 1

    def test_runs_without_the_stamp_are_ignored(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            succeed_first_pass(experiment.begin_task("a", unit=treatment_unit(experiment)))
            plain = aer.start_run(task="outside any experiment")
            with plain.tool("wp.update_page") as tool:
                tool.set_result({"status": 200})
            plain.success()

            report = compute_report(aer, name="pilot")

            assert report.treatment.runs == 1
            assert report.control.runs == 0


class TestIndicators:
    def test_success_and_first_pass_rates(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            # control: one clean success, one recovery success, one open failure
            succeed_first_pass(experiment.begin_task("c1", unit=control_unit(experiment, 0)))
            succeed_after_recovery(experiment.begin_task("c2", unit=control_unit(experiment, 1)))
            fail_openly(experiment.begin_task("c3", unit=control_unit(experiment, 2)))

            report = compute_report(aer, name="pilot")
            control = report.control

            assert control.runs == 3
            assert control.success_rate == pytest.approx(2 / 3)
            assert control.first_pass_success_rate == pytest.approx(1 / 3)
            assert control.recovery_rate == pytest.approx(1 / 3)
            # None of the control runs were verified by an independent check.
            assert control.verified_success_rate == 0.0

    def test_tool_calls_and_duration(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            succeed_first_pass(experiment.begin_task("c1", unit=control_unit(experiment, 0)))
            succeed_after_recovery(experiment.begin_task("c2", unit=control_unit(experiment, 1)))

            report = compute_report(aer, name="pilot")

            assert report.control.avg_tool_calls == pytest.approx((1 + 2) / 2)
            assert report.control.avg_duration_seconds is not None
            assert report.control.avg_duration_seconds >= 0.0

    def test_human_intervention(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            succeed_first_pass(experiment.begin_task("c1", unit=control_unit(experiment, 0)))
            steered = experiment.begin_task("c2", unit=control_unit(experiment, 1))
            succeed_first_pass(steered)
            add_human_feedback(aer, steered.run.run_id, approved=True)

            report = compute_report(aer, name="pilot")

            assert report.control.human_intervention_rate == pytest.approx(0.5)


class TestExperienceColumns:
    def test_hit_rate_and_assisted_success(self, data_dir: Path) -> None:
        index = RecordingIndex()
        with open_aer(data_dir, index) as aer:
            give_index_a_hit(aer, index)
            experiment = Experiment(aer, name="pilot", seed=7)
            guided = experiment.begin_task("t1", unit=treatment_unit(experiment, 0))
            tracked = guided.retrieve("how do I avoid the 403")
            assert tracked is not None
            succeed_first_pass(guided)
            # A treatment run that never asked for experience: no hit, still counted.
            succeed_first_pass(experiment.begin_task("t2", unit=treatment_unit(experiment, 1)))

            report = compute_report(aer, name="pilot")

            assert report.treatment.retrieval_sessions == 1
            assert report.treatment.experience_hit_rate == pytest.approx(0.5)
            assert report.treatment.assisted_success_rate == pytest.approx(1.0)
            # The control arm never retrieves, and the report says so with None
            # rather than a flattering 0.
            assert report.control.retrieval_sessions == 0
            assert report.control.experience_hit_rate is None
            assert report.control.assisted_success_rate is None

    def test_assisted_success_counts_failures_too(self, data_dir: Path) -> None:
        index = RecordingIndex()
        with open_aer(data_dir, index) as aer:
            give_index_a_hit(aer, index)
            experiment = Experiment(aer, name="pilot", seed=7)
            guided = experiment.begin_task("t1", unit=treatment_unit(experiment, 0))
            guided.retrieve("how do I avoid the 403")
            fail_openly(guided)

            report = compute_report(aer, name="pilot")

            assert report.treatment.assisted_success_rate == pytest.approx(0.0)


class TestEmptyAndLift:
    def test_empty_arms_report_none_not_zero(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            report = compute_report(aer, name="never-ran")

        assert report.control.runs == 0
        assert report.control.success_rate is None
        assert report.control.avg_tool_calls is None
        assert report.control.avg_duration_seconds is None
        assert report.success_lift is None

    def test_lift_is_treatment_minus_control(self, data_dir: Path) -> None:
        with open_aer(data_dir) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            succeed_first_pass(experiment.begin_task("c", unit=control_unit(experiment, 0)))
            fail_openly(experiment.begin_task("c2", unit=control_unit(experiment, 1)))
            succeed_first_pass(experiment.begin_task("t", unit=treatment_unit(experiment, 0)))

            report = compute_report(aer, name="pilot")

            assert report.control.success_rate == pytest.approx(0.5)
            assert report.treatment.success_rate == pytest.approx(1.0)
            assert report.success_lift == pytest.approx(0.5)
