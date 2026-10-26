"""The experiment harness: assignment, stamped runs, arm-aware retrieval."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from aer.runtime.enums import RetrievalMode, SessionAssignment
from aer.runtime.run import RunContext
from aer.runtime.serialization import to_json_object
from aer.usage.tracking import TrackedRetrievalResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from aer.runtime.runtime import AER

#: Arm label stamped onto control runs. Free metadata text, deliberately not a
#: :class:`~aer.runtime.enums.SessionAssignment` value: the enum describes
#: *sessions*, and a control run has no sessions at all.
CONTROL = "control"

#: Arm label stamped onto treatment runs.
TREATMENT = "treatment"


class Experiment:
    """One named A/B experiment over an open :class:`~aer.runtime.runtime.AER`.

    Assignment is a pure function of ``(seed, unit)``: same unit, same arm, every
    time. The unit is whatever identifies a task across arms -- a task id, a
    ticket number -- so both arms execute *the same* task population and the
    comparison is within-task, not between different tasks.
    """

    def __init__(self, runtime: AER, *, name: str, seed: int = 0) -> None:
        if not name.strip():
            raise ValueError("An experiment needs a non-empty name")
        self._runtime = runtime
        self._name = name
        self._seed = seed

    @property
    def name(self) -> str:
        """The experiment name; also the ``experiment_id`` stamped on sessions."""
        return self._name

    @property
    def seed(self) -> int:
        """The assignment seed. Changing it re-randomises every arm."""
        return self._seed

    def arm_for(self, unit: str) -> str:
        """Which arm ``unit`` belongs to: ``"control"`` or ``"treatment"``.

        The hash is deterministic and process-independent, so the assignment can
        be re-derived and audited from ``(seed, unit)`` alone -- the property that
        makes a disputed assignment arguable after the fact.
        """
        digest = hashlib.sha256(f"{self._seed}:{unit}".encode()).hexdigest()
        return TREATMENT if int(digest[:8], 16) % 2 == 0 else CONTROL

    def begin_task(
        self,
        task: str,
        *,
        unit: str | None = None,
        task_type: str | None = None,
        agent_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ExperimentTask:
        """Open a stamped run for one task execution and return its handle.

        The run's metadata carries ``experiment = {name, assignment, unit}`` so
        :func:`~aer.experiment.metrics.compute_report` can find every run of this
        experiment without a schema change. Extra caller metadata is merged under
        it, never alongside the experiment block.
        """
        assigned_unit = unit if unit is not None else task
        arm = self.arm_for(assigned_unit)
        stamped: dict[str, object] = {
            **dict(metadata or {}),
            "experiment": {
                "name": self._name,
                "assignment": arm,
                "unit": assigned_unit,
            },
        }
        run = self._runtime.start_run(
            task=task,
            task_type=task_type,
            agent_name=agent_name,
            metadata=to_json_object(stamped),
        )
        return ExperimentTask(experiment=self, run=run, assignment=arm, unit=assigned_unit)


class ExperimentTask:
    """One task execution inside an experiment arm.

    The executor drives ``run`` exactly as it would outside an experiment --
    ``with task.run.tool(...)``, ``task.run.success()`` -- and asks for experience
    through :meth:`retrieve`, never directly. On the control arm :meth:`retrieve`
    returns ``None`` and records nothing: the executor code is identical in both
    arms, and the store shows the difference.
    """

    def __init__(
        self, *, experiment: Experiment, run: RunContext, assignment: str, unit: str
    ) -> None:
        self._experiment = experiment
        self.run = run
        self.assignment = assignment
        self.unit = unit

    @property
    def is_treatment(self) -> bool:
        """Whether this task may see retrieved experience."""
        return self.assignment == TREATMENT

    def retrieve(
        self,
        query: str,
        *,
        domain: str | None = None,
        mode: RetrievalMode = RetrievalMode.GUIDANCE,
        limit: int = 3,
    ) -> TrackedRetrievalResult | None:
        """Experience relevant to ``query``, or ``None`` on the control arm.

        The treatment call goes through
        :meth:`~aer.runtime.runtime.AER.retrieve_for_run`, so the session is
        recorded with ``experiment_id`` and ``assignment=TREATMENT`` -- the
        per-session provenance the holdout analysis of TASKS.md #32 was designed
        around. The control call is not recorded at all: nothing was retrieved.

        Raises:
            KnowledgeIndexUnavailable: the treatment arm has no reachable index.
                Deliberately not an empty answer -- a treatment run that could
                not check the knowledge base is not a treatment run.
        """
        if not self.is_treatment:
            return None
        return self._experiment._runtime.retrieve_for_run(
            query,
            run_id=self.run.run_id,
            domain=domain,
            mode=mode,
            limit=limit,
            experiment_id=self._experiment.name,
            assignment=SessionAssignment.TREATMENT,
        )
