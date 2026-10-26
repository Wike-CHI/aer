"""``Experiment`` -- the A/B harness behind TASKS.md #17-#19.

The MVP hypothesis is::

    P(success | experience) > P(success | no experience)

Testing it needs three things this package provides:

* **deterministic assignment** -- :meth:`Experiment.arm_for` hashes ``seed:unit``
  so the same task always lands in the same arm, across processes and reruns.
  Reproducibility is what makes a failed experiment arguable: anyone can re-derive
  who was in which arm;
* **a single task entry point** -- :meth:`Experiment.begin_task` opens the run,
  stamps the experiment into its metadata, and returns an
  :class:`ExperimentTask` whose :meth:`ExperimentTask.retrieve` returns tracked
  retrieval results on the treatment arm and ``None`` on control. The executor
  code is *identical* in both arms; the holdout is enforced by the API shape,
  not by the executor knowing which arm it is in;
* **no silent leakage** -- control runs get no retrieval session at all, so the
  store itself records "nothing was retrieved" rather than relying on the caller
  to remember. (A caller that retrieves behind the harness' back can of course
  break the holdout; the harness cannot sandbox its own caller. Holdout
  cleanliness is a protocol obligation, the same way ``run.success()`` is.)

What this package deliberately is **not**: proof of the hypothesis. The pilot in
``scripts/run_experiment_pilot.py`` and the tests run a *scripted* executor to
validate the harness end to end; TASKS.md #17 reserves the real experiment for the
real WordPress agent, 50+ tasks per arm.
"""

from aer.experiment.harness import Experiment, ExperimentTask
from aer.experiment.metrics import ArmMetrics, ExperimentReport, compute_report

__all__ = [
    "ArmMetrics",
    "Experiment",
    "ExperimentReport",
    "ExperimentTask",
    "compute_report",
]
