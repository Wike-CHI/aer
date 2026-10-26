"""The A/B harness: deterministic assignment and arm-aware retrieval (TASKS #17).

The hypothesis under test (TASKS #19) is ``P(success | experience) > P(success)``.
These tests pin the mechanism that makes such a test fair: assignment must be a
pure function of ``(seed, unit)``, the executor code path must be identical in
both arms, and the store -- not the caller's memory -- must record which arm a
retrieval belonged to.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from aer import AER, Experiment, SessionAssignment
from aer.exceptions import KnowledgeIndexUnavailable
from tests.knowledge.support import RecordingIndex
from tests.usage.support import indexed_for, script_retrieval


def open_aer(data_dir: Path, index: RecordingIndex) -> AER:
    return AER(data_dir, knowledge_index=index)


def give_index_a_hit(aer: AER, index: RecordingIndex) -> None:
    """Script one hit that points at a real stored experience.

    A hit for an experience SQLite does not have is a foreign-key violation, not
    a scenario -- usage rows point at the store's row on purpose.
    """
    script_retrieval(index, guidance=[indexed_for(aer)])


class TestAssignment:
    def test_arm_for_is_deterministic(self, data_dir: Path) -> None:
        with open_aer(data_dir, RecordingIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)

            assert experiment.arm_for("task-1") == experiment.arm_for("task-1")

    def test_both_arms_occur(self, data_dir: Path) -> None:
        with open_aer(data_dir, RecordingIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)

            arms = {experiment.arm_for(f"task-{i}") for i in range(50)}

            assert arms == {"control", "treatment"}

    def test_a_different_seed_reassigns(self, data_dir: Path) -> None:
        """Not guaranteed for any single unit -- guaranteed across enough of them."""
        with open_aer(data_dir, RecordingIndex()) as aer:
            first = Experiment(aer, name="pilot", seed=1)
            second = Experiment(aer, name="pilot", seed=2)

            flips = sum(
                first.arm_for(f"task-{i}") != second.arm_for(f"task-{i}") for i in range(50)
            )

            assert 0 < flips < 50

    def test_name_is_required(self, data_dir: Path) -> None:
        with (
            open_aer(data_dir, RecordingIndex()) as aer,
            pytest.raises(ValueError, match="non-empty name"),
        ):
            Experiment(aer, name="  ")


class TestBeginTask:
    def test_the_run_is_stamped(self, data_dir: Path) -> None:
        with open_aer(data_dir, RecordingIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            task = experiment.begin_task("fix the page", unit="task-1", task_type="wp")

            stored = aer.get_run(task.run.run_id)
            assert stored is not None
            stamp = stored.metadata["experiment"]
            assert stamp["name"] == "pilot"
            assert stamp["assignment"] == task.assignment
            assert stamp["unit"] == "task-1"

    def test_unit_defaults_to_the_task_text(self, data_dir: Path) -> None:
        with open_aer(data_dir, RecordingIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            task = experiment.begin_task("fix the page")

            assert task.unit == "fix the page"

    def test_caller_metadata_is_kept_alongside_the_stamp(self, data_dir: Path) -> None:
        with open_aer(data_dir, RecordingIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            task = experiment.begin_task(
                "fix the page", unit="task-1", metadata={"lane": "nightly"}
            )

            stored = aer.get_run(task.run.run_id)
            assert stored is not None
            assert stored.metadata["lane"] == "nightly"
            assert stored.metadata["experiment"]["name"] == "pilot"


class TestArmAwareRetrieval:
    def test_treatment_retrieves_and_records_the_assignment(self, data_dir: Path) -> None:
        index = RecordingIndex()
        with open_aer(data_dir, index) as aer:
            give_index_a_hit(aer, index)
            experiment = Experiment(aer, name="pilot", seed=7)
            treatment_unit = next(
                f"task-{i}" for i in range(50) if experiment.arm_for(f"task-{i}") == "treatment"
            )
            task = experiment.begin_task("fix the page", unit=treatment_unit)

            tracked = task.retrieve("how do I avoid the 403")

            assert tracked is not None
            assert len(tracked.result.all_hits) == 1
            session = aer.retrieval_sessions.get(tracked.session_id)
            assert session is not None
            assert session.experiment_id == "pilot"
            assert session.assignment is SessionAssignment.TREATMENT
            assert session.run_id == task.run.run_id

    def test_control_gets_none_and_records_nothing(self, data_dir: Path) -> None:
        index = RecordingIndex()
        with open_aer(data_dir, index) as aer:
            give_index_a_hit(aer, index)
            experiment = Experiment(aer, name="pilot", seed=7)
            control_unit = next(
                f"task-{i}" for i in range(50) if experiment.arm_for(f"task-{i}") == "control"
            )
            task = experiment.begin_task("fix the page", unit=control_unit)
            assert not task.is_treatment

            tracked = task.retrieve("how do I avoid the 403")

            assert tracked is None
            assert aer.retrieval_sessions.count() == 0
            assert index.searches == []  # the index was never even asked

    def test_an_unreachable_index_is_not_an_empty_answer(self, data_dir: Path) -> None:
        class DownIndex(RecordingIndex):
            def search(self, query):  # type: ignore[no-untyped-def]
                raise KnowledgeIndexUnavailable("index down")

        with open_aer(data_dir, DownIndex()) as aer:
            experiment = Experiment(aer, name="pilot", seed=7)
            treatment_unit = next(
                f"task-{i}" for i in range(50) if experiment.arm_for(f"task-{i}") == "treatment"
            )
            task = experiment.begin_task("fix the page", unit=treatment_unit)

            with pytest.raises(KnowledgeIndexUnavailable):
                task.retrieve("how do I avoid the 403")


class TestTheExecutorIsArmBlind:
    def test_one_executor_function_serves_both_arms(self, data_dir: Path) -> None:
        """The point of the harness shape: the executor never branches on the arm,
        it branches on whether experience *was returned* -- which is exactly the
        causal difference the experiment measures."""

        def execute(task: object) -> None:
            guidance = task.retrieve("how do I avoid the 403")  # type: ignore[attr-defined]
            with task.run.tool("wp.update_page") as tool:  # type: ignore[attr-defined]
                if guidance is None:
                    tool.set_result({"status": 403})
                    raise PermissionError("403")
                tool.set_result({"status": 200})

        index = RecordingIndex()
        with open_aer(data_dir, index) as aer:
            give_index_a_hit(aer, index)
            experiment = Experiment(aer, name="pilot", seed=7)
            for i in range(20):
                task = experiment.begin_task("fix the page", unit=f"task-{i}")
                with contextlib.suppress(PermissionError):
                    execute(task)
                task.run.success()

            # The control runs recorded a failure the treatment runs never saw.
            from aer.runtime.enums import EventType

            control_runs = treatment_runs = 0
            control_errors = 0
            for run in aer.list_runs(limit=100):
                stamp = run.metadata.get("experiment", {})
                if not isinstance(stamp, dict) or stamp.get("name") != "pilot":
                    continue
                errors = sum(1 for e in aer.get_events(run.id) if e.event_type is EventType.ERROR)
                if stamp.get("assignment") == "control":
                    control_runs += 1
                    control_errors += errors
                else:
                    treatment_runs += 1
                    assert errors == 0
            assert control_runs and treatment_runs  # both arms were exercised
            assert control_errors == control_runs
