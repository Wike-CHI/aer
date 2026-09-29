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

#: Statuses a passing verification can promote to a verified success.
#:
#: ``SUCCESS`` is a declaration the verification confirms; ``INCONCLUSIVE`` is the
#: absence of a declaration, which a verification *replaces*. Everything else is a
#: declaration that the work was not completed, and no passing check changes that.
_SUCCESS_STATUSES = frozenset({RunStatus.SUCCESS, RunStatus.INCONCLUSIVE})


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
        required = [record for record in records if record.required]
        return cls.from_counts(
            run_id,
            total=len(records),
            passed=sum(1 for record in records if record.passed),
            required_total=len(required),
            required_passed=sum(1 for record in required if record.passed),
        )

    @classmethod
    def from_counts(
        cls,
        run_id: str,
        *,
        total: int,
        passed: int,
        required_total: int,
        required_passed: int,
    ) -> VerificationSummary:
        """Build a summary from pre-aggregated counts.

        The second entry point exists for the effectiveness report, which needs the
        summary of every run beneath a set of usage rows. Reading each run's verdicts
        and calling :meth:`from_records` would be the N+1 that round-7 brief section
        56 rules out, and re-deriving ``pass_rate`` / ``all_passed`` at the call site
        would put the definition of a verified success in two places. Both callers
        therefore funnel through this constructor.
        """
        failed = total - passed
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

    Two statuses can reach it, and the second one is the whole point of
    ``INCONCLUSIVE`` (round-8.1.1, D-100)::

        verified_success  ==  (Run.status is SUCCESS or Run.status is INCONCLUSIVE)
                              AND at least one required verification exists
                              AND no required verification failed

    Why ``INCONCLUSIVE`` qualifies: the clause exists to stop an *agent's own claim*
    from being mistaken for evidence of the work. An ``INCONCLUSIVE`` run contains no
    claim at all -- nobody declared anything -- so the required verification is not
    competing with a declaration, it is **substituting** for one. Requiring a
    declaration first would mean that the integrations which cannot state an outcome
    (a CLI whose session hooks carry no result) could never produce a verified success
    even when an independent check proved the task was done: the evidence would be
    there and the vocabulary would have no way to say so.

    Why the other statuses do not: ``FAILED``, ``ABORTED`` and ``PARTIAL_SUCCESS`` are
    declarations that the work was *not* completed. Passing checks show the
    environment is fine, not that the agent finished, so no verification can promote
    one of them.

    Each remaining clause rejects a distinct lie:

    * ``required_total == 0`` rejects "verified" by absence of evidence;
    * ``required_failed == 0`` is the actual confirmation.

    This is a **derived** predicate, never stored on the run. It must be recomputed
    after new verdicts arrive, which is exactly what makes it trustworthy.
    """
    if status not in _SUCCESS_STATUSES:
        return False
    return summary.required_total > 0 and summary.required_failed == 0
