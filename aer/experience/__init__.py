"""Experience core: turning a finished run into reusable, evidence-backed knowledge.

The pipeline, and where each guarantee comes from::

    Run + Events + Errors + Recoveries + Verifications     (storage/repositories)
        -> RunEvidence                                     (evidence.py)
        -> DistillationPolicy                              (policy.py: is it worth it?)
        -> DistillationProvider                            (provider.py: the brain)
        -> ExperienceCandidate                             (candidate.py: a proposal)
        -> classify_kind                                   (classify.py: the facts)
        -> dedup_key_for                                   (dedup.py: same claim?)
        -> Experience + ExperienceSource                   (service.py: persisted)
        -> RAW/DISTILLED/VERIFIED                          (lifecycle.py)

The division of responsibility is the point of the milestone:

* the **provider** explains a trajectory in prose, and may be a language model;
* the **system** decides what kind of experience it is, whether its outcome is
  verified, what status it holds and whether it duplicates existing knowledge.

The provider is therefore never in a position to turn "the agent believed it
succeeded" into stored success (``docs/DECISIONS.md`` D-030 to D-033).

Not here, deliberately: retrieval and ranking (Milestone 6), and experience usage,
success statistics and confidence calibration (Milestone 7).
"""

from aer.experience.candidate import (
    ExperienceCandidate,
    normalise_candidate,
    validate_candidate,
)
from aer.experience.classify import classify_kind, outcome_is_verified
from aer.experience.dedup import dedup_key_for, normalise_text
from aer.experience.distiller import ExperienceDistiller
from aer.experience.evidence import RunEvidence, RunEvidenceBuilder
from aer.experience.policy import DEFAULT_POLICY, DistillationDecision, DistillationPolicy
from aer.experience.provider import CallableDistillationProvider, DistillationProvider
from aer.experience.service import UNCONFIGURED, ExperienceService

__all__ = [
    "DEFAULT_POLICY",
    "UNCONFIGURED",
    "CallableDistillationProvider",
    "DistillationDecision",
    "DistillationPolicy",
    "DistillationProvider",
    "ExperienceCandidate",
    "ExperienceDistiller",
    "ExperienceService",
    "RunEvidence",
    "RunEvidenceBuilder",
    "classify_kind",
    "dedup_key_for",
    "normalise_candidate",
    "normalise_text",
    "outcome_is_verified",
    "validate_candidate",
]
