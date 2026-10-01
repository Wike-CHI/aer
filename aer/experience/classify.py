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

A run's ending is one of three things, and the third one used to be missing from the
vocabulary (round-8.1.1, D-100):

* the agent declared an outcome (``SUCCESS`` / ``PARTIAL_SUCCESS`` / ``FAILED`` /
  ``ABORTED``);
* nobody declared anything, but an independent verification decided it anyway --
  which is how an integration that cannot report an outcome still produces experience;
* nobody declared anything and nothing decided it, in which case **no kind exists**
  and this module says so instead of picking one.
"""

from __future__ import annotations

from aer.exceptions import DistillationError
from aer.experience.evidence import RunEvidence
from aer.runtime.enums import ExperienceKind, RunStatus


def classify_kind(evidence: RunEvidence) -> ExperienceKind:
    """Decide which kind of experience this run can become.

    Precedence, highest first:

    =======================================================  ===========  =============
    condition                                                kind         why it wins
    =======================================================  ===========  =============
    a required verification failed                           ``FAILURE``  an independent
                                                                          check proved
                                                                          the goal is
                                                                          *not* met
    the run declared a non-success                           ``FAILURE``  the agent
                                                                          itself did
                                                                          not claim
                                                                          completion
    nobody declared anything (``INCONCLUSIVE``)              *(no kind)*  silence is
                                                                          not a claim
    a failure was recorded and then repaired                 ``RECOVERY`` the trajectory
                                                                          contains a
                                                                          fix
    otherwise                                                ``SUCCESS``  the outcome
                                                                          was claimed
                                                                          or proven,
                                                                          and nothing
                                                                          contradicted it
    =======================================================  ===========  =============

    ``INCONCLUSIVE`` is where the two answers meet, and it is decided by evidence
    rather than by the ending (round-8.1.1, D-100):

    * **with** an independent confirmation -- every required check passed -- the run is
      a ``SUCCESS`` (or a ``RECOVERY``, if the trajectory contains a repair). The
      missing declaration is replaced by something stronger than a declaration, which
      is what :func:`~aer.verification.summary.is_verified_success` encodes;
    * **without** one, there is no kind to return. The rule above is therefore not
      merely unhit, it is unreachable-by-design: an ``INCONCLUSIVE`` run that reaches
      the ``FAILURE`` branches does so only because a required check *failed*, which is
      a proven non-success rather than an assumed one.

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
        DistillationError: the run finished without anyone declaring an outcome and
            without any required verification deciding one. There is nothing to
            classify, and returning ``FAILURE`` would be the fabrication this milestone
            exists to remove.
    """
    if not evidence.is_finished:
        raise DistillationError(
            f"Run {evidence.run_id} is still {evidence.run.status.value}; "
            "an unfinished run has no outcome to distil"
        )

    if evidence.required_failed > 0:
        return ExperienceKind.FAILURE

    if evidence.run.status is RunStatus.INCONCLUSIVE:
        if not evidence.verified_success:
            # Nobody declared an outcome, and nothing independently decided one, so
            # there is no claim to classify. The policy vetoes these runs before
            # reaching here; this guard exists so that a caller reaching classify_kind
            # directly gets an explanation rather than a fabricated ``FAILURE``
            # (round-8.1 sections 13, round-8.1.1 section 4).
            raise DistillationError(
                f"Run {evidence.run_id} was closed without anyone declaring an outcome "
                "and without a required verification deciding one; it has no kind to "
                "classify. 'Nobody said anything' is not 'it failed'."
            )
        # Verified: the outcome was established by evidence, so the run is classified
        # exactly as a declared success would be, below.
    elif evidence.run.status is not RunStatus.SUCCESS:
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
