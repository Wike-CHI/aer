"""Run-level aggregation of verdicts, and the derived ``verified_success``.

A summary is a pure function of the recorded verdicts plus the run's own status.
It is not a status: AER does **not** add a ``VERIFIED`` member to
:class:`~aer.runtime.enums.RunStatus`. ``Run.status`` records what the agent
declared; verification is a separate fact that is *combined* with it on demand. If
verification were folded into the status, the historical claim would be destroyed
the moment a verifier disagreed -- and the whole point of this milestone is to keep
both facts side by side (round-4 brief, sections 5 and 24).

Two aggregates are reported, and the difference matters:

* ``all_passed`` / ``all_required_passed``-style booleans over **everything**
  answer "did any check fail?";
* ``verified_success`` answers "was this task done *and* independently confirmed?",
  over required checks only, so an optional quality score cannot veto a proven task.

An empty verdict list is never "all passed": ``total == 0`` yields
``all_passed = False`` and ``verified_success = False``. "Nobody checked" is not
"everything is fine".
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from aer.runtime.enums import RunStatus
from aer.runtime.models import VerificationRecord


class VerificationSummary(BaseModel):
    """How the verdicts recorded for one run add up.

    ``pass_rate`` is ``None`` -- not ``1.0``, not ``0.0`` -- when there is nothing
    to average, so a caller that only looks at the number cannot mistake an
    unverified run for a clean one.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)

    required_total: int = Field(ge=0)
    required_passed: int = Field(ge=0)
    required_failed: int = Field(ge=0)

    pass_rate: float | None = None

    all_passed: bool
    """``total > 0 and failed == 0`` -- over every verdict, optional included."""

    all_required_passed: bool
    """``required_total > 0 and required_failed == 0``."""

    @classmethod
    def from_records(
        cls, run_id: str, records: Sequence[VerificationRecord]
    ) -> VerificationSummary:
        """Aggregate ``records`` for ``run_id``."""
        total = len(records)
        passed = sum(1 for record in records if record.passed)
        failed = total - passed

        required = [record for record in records if record.required]
        required_total = len(required)
        required_passed = sum(1 for record in required if record.passed)
        required_failed = required_total - required_passed

        return cls(
            run_id=run_id,
            total=total,
            passed=passed,
            failed=failed,
            required_total=required_total,
            required_passed=required_passed,
            required_failed=required_failed,
            pass_rate=None if total == 0 else passed / total,
            all_passed=total > 0 and failed == 0,
            all_required_passed=required_total > 0 and required_failed == 0,
        )


def is_verified_success(status: RunStatus, summary: VerificationSummary) -> bool:
    """Whether a run can be called a *verified* success.

    The one definition, in one place::

        verified_success  ==  Run.status is SUCCESS
                              AND at least one required verification exists
                              AND no required verification failed

    Each clause rejects a distinct lie:

    * a ``FAILED`` or ``ABORTED`` run is not a success no matter what passed --
      passing checks show the environment is fine, not that the agent finished;
    * ``required_total == 0`` rejects "verified" by absence of evidence;
    * ``required_failed == 0`` is the actual confirmation.

    This is a **derived** predicate, never stored on the run. It must be recomputed
    after new verdicts arrive, which is exactly what makes it trustworthy.
    """
    return (
        status is RunStatus.SUCCESS and summary.required_total > 0 and summary.required_failed == 0
    )
