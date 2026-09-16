"""Which kind of experience a run produces -- decided by facts, not by a model.

This is the module that makes section 27 of the round-5 brief true in code::

    The distiller may explain a trajectory. It does not get to decide whether the
    task succeeded.

The reason is not purity, it is that the answer is already known. The runtime
records whether the agent declared success (``Run.status``) and whether an
independent verifier agreed (Milestone 4's ``verified_success``). Asking a language
model to classify the run again would replace a recorded fact with an opinion, and
the one case that must never be misclassified -- an agent that believes it succeeded
while the environment disagrees -- is exactly the case where a plausible-sounding
model would side with the agent's own narrative (brief section 54).

The precedence below is total: every finished run maps to exactly one kind, and no
branch is reachable two ways.
"""

from __future__ import annotations

from aer.exceptions import DistillationError
from aer.experience.evidence import RunEvidence
from aer.runtime.enums import ExperienceKind, RunStatus


def classify_kind(evidence: RunEvidence) -> ExperienceKind:
    """Decide which kind of experience this run can become.

    Precedence, highest first:

    ==========================================  ===========  =========================
    condition                                   kind         why it wins
    ==========================================  ===========  =========================
    a required verification failed              ``FAILURE``  an independent check proved
                                                             the goal is *not* met, so
                                                             nothing else can matter
    the run did not end in ``SUCCESS``          ``FAILURE``  the agent itself did not
                                                             claim completion
    a failure was recorded and then repaired    ``RECOVERY`` the trajectory contains a
                                                             fix, which is the most
                                                             reusable thing AER has
    otherwise                                   ``SUCCESS``  the agent claimed success
                                                             and no check contradicted it
    ==========================================  ===========  =========================

    Two consequences worth stating explicitly, because both look like omissions:

    * **Unverified success still classifies as ``SUCCESS``.** "The agent says it
      succeeded and nobody checked" is not the same as "it failed", and calling it a
      failure would put a claim in the store that the environment never made. The
      missing trust is carried by
      :attr:`~aer.runtime.models.Experience.outcome_verified` and by the status
      stopping at ``DISTILLED`` instead of reaching ``VERIFIED`` (D-031).
    * **Recovery is decided by the trajectory, not by verification.** A repaired run
      whose result was never checked is still a ``RECOVERY``: the fix happened, and
      discarding that because no verifier ran would throw away the single most
      valuable kind of evidence (brief sections 2 and 18).

    Raises:
        DistillationError: the run has not finished. An unfinished run has no outcome
            to classify, and guessing one would store a verdict about a situation
            that is still changing.
    """
    if not evidence.is_finished:
        raise DistillationError(
            f"Run {evidence.run_id} is still {evidence.run.status.value}; "
            "an unfinished run has no outcome to distil"
        )

    if evidence.required_failed > 0:
        return ExperienceKind.FAILURE
    if evidence.run.status is not RunStatus.SUCCESS:
        # Covers FAILED, ABORTED and PARTIAL_SUCCESS: "partly done" is a form of "not
        # done", and AER has no fourth kind to put it in. If one is ever wanted it has
        # to be an explicit brief change, not an inference made here.
        return ExperienceKind.FAILURE
    if evidence.has_successful_recovery:
        return ExperienceKind.RECOVERY
    return ExperienceKind.SUCCESS


def outcome_is_verified(kind: ExperienceKind, evidence: RunEvidence) -> bool:
    """Whether the run's *outcome* is backed by independent evidence.

    The mirror of :func:`classify_kind`, and deliberately asymmetric (brief sections
    32-33):

    * for ``SUCCESS`` / ``RECOVERY`` -- verified when every required check passed.
      That is what makes "the goal was met" an observed fact rather than a claim;
    * for ``FAILURE`` -- verified only when a required verification actually failed.
      A run whose agent simply declared failure is *not* independently verified: the
      agent's own verdict is not evidence, exactly as Milestone 4 refuses to treat an
      agent's ``SUCCESS`` as verification.

    What this flag never claims is that the *explanation* is right. A verified
    outcome plus ``root_cause`` is still a hypothesis about why (D-033).
    """
    if kind is ExperienceKind.FAILURE:
        return evidence.required_failed > 0
    return evidence.verified_success
