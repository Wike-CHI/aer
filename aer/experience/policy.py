"""``DistillationPolicy`` -- is this run worth spending a distillation pass on?

The policy answers exactly one question and nothing else (brief section 15):

    Should this run go to the distiller?

It does **not** distil, it does not classify beyond delegating to
:func:`~aer.experience.classify.classify_kind`, and it does not touch storage. It
reads a :class:`~aer.experience.evidence.RunEvidence` and returns a decision with the
reasoning attached, so a future cost model can audit whether the policy earns its
tokens.

The bias is deliberate and stated in agent.md #22: a plain, verified, uneventful
success teaches nothing, and filling the store with "normal execution worked" makes
retrieval worse rather than better. Interesting runs are the ones where something
went wrong, something was repaired, a human intervened, or the agent's own summary
of events does not match what an independent check found.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aer.experience.classify import classify_kind
from aer.experience.evidence import RunEvidence
from aer.runtime.enums import DistillationTrigger, ExperienceKind, RunStatus

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class DistillationDecision(BaseModel):
    """Whether to distil, which kind, and why.

    ``kind`` is ``None`` exactly when ``should_distill`` is ``False``: a decision not
    to distil has no kind, because no experience will be produced. It is *not*
    "the kind we would have used", which would invite a caller to ignore the
    decision.
    """

    model_config = _FROZEN

    run_id: str
    should_distill: bool
    kind: ExperienceKind | None = None
    triggers: tuple[DistillationTrigger, ...] = ()
    reasons: tuple[str, ...] = ()
    """Human readable justification, one entry per trigger plus any veto."""

    @property
    def is_veto(self) -> bool:
        """``True`` when the run was refused rather than merely uninteresting."""
        return not self.should_distill and bool(self.reasons)

    def __repr__(self) -> str:
        kind = self.kind.value if self.kind is not None else "-"
        triggers = ",".join(trigger.value for trigger in self.triggers) or "-"
        return (
            f"DistillationDecision(run_id={self.run_id!r}, "
            f"should_distill={self.should_distill}, kind={kind}, triggers={triggers})"
        )


class DistillationPolicy:
    """Decides which runs become experiences.

    Stateless and deterministic: the same evidence always yields the same decision,
    which is what lets the pipeline be tested without a model in the loop.
    """

    name = "default"

    def evaluate(
        self,
        evidence: RunEvidence,
        *,
        explicit_high_value: bool = False,
    ) -> DistillationDecision:
        """Decide whether ``evidence`` should be distilled.

        Args:
            evidence: The assembled run package.
            explicit_high_value: The caller asked to keep this run regardless of the
                signals (brief sections 16-17). The only way an uneventful verified
                success gets distilled.

        Returns:
            A decision. ``should_distill=False`` is a normal outcome, not an error:
            most runs are uninteresting, and saying so is the policy's job.
        """
        if not evidence.is_finished:
            return self._veto(
                evidence,
                f"run is still {evidence.run.status.value}; an unfinished run has no outcome",
            )

        triggers: list[DistillationTrigger] = []
        reasons: list[str] = []

        if evidence.has_errors:
            triggers.append(DistillationTrigger.ERROR)
            reasons.append(f"{len(evidence.errors)} error(s) recorded")

        if evidence.recoveries:
            triggers.append(DistillationTrigger.RECOVERY)
            reasons.append(f"{len(evidence.recoveries)} recovery attempt(s) recorded")

        if evidence.has_human_feedback:
            triggers.append(DistillationTrigger.HUMAN_FEEDBACK)
            reasons.append(f"{len(evidence.human_feedback_events)} human feedback observation(s)")

        if evidence.required_failed > 0:
            triggers.append(DistillationTrigger.VERIFICATION_FAILURE)
            reasons.append(f"{evidence.required_failed} required verification(s) failed")

        if evidence.run.status is not RunStatus.SUCCESS:
            triggers.append(DistillationTrigger.FAILED_RUN)
            reasons.append(f"run ended {evidence.run.status.value}")

        if evidence.run.status is RunStatus.SUCCESS and not evidence.verified_success:
            triggers.append(DistillationTrigger.UNVERIFIED_SUCCESS)
            reasons.append("the agent declared success but no required verification confirms it")

        if explicit_high_value:
            triggers.append(DistillationTrigger.EXPLICIT_HIGH_VALUE)
            reasons.append("caller marked this run as high value")

        if not triggers:
            return self._veto(
                evidence,
                "plain verified success with nothing unusual; nothing to learn",
            )

        return DistillationDecision(
            run_id=evidence.run_id,
            should_distill=True,
            kind=classify_kind(evidence),
            triggers=tuple(triggers),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _veto(evidence: RunEvidence, reason: str) -> DistillationDecision:
        """A decision not to distil, with the reason kept for observability."""
        return DistillationDecision(
            run_id=evidence.run_id,
            should_distill=False,
            kind=None,
            triggers=(),
            reasons=(reason,),
        )


#: Shared instance. The policy is stateless, so one instance is enough.
DEFAULT_POLICY = DistillationPolicy()
