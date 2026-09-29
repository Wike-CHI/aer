"""Experience usage: did any of this knowledge actually help? (Milestone 7)

The pipeline this package closes::

    Experience
       -> Retrieved
       -> Injected
       -> Adopted / Ignored / Rejected
       -> Agent execution
       -> Verification
       -> Observed effectiveness
       -> Experience lifecycle

Four distinctions are the entire subject matter, and every module here exists to keep
one of them from collapsing (round-7 brief, section 2)::

    Retrieved != Injected      a result nobody rendered was not used
    Injected  != Used          something in a context is not something adopted
    Used      != Helpful       an agent can adopt a wrong experience
    Success   != Effect        a task succeeding is not proof the experience caused it

What is where:

* :mod:`~aer.usage.fingerprints` -- sanitising a query and digesting a context, so
  that neither the prompt nor the rendered text is ever stored;
* :mod:`~aer.usage.tracking` -- writing the facts: sessions, injections, signals,
  utility labels, run attachment. The only module in AER that writes usage;
* :mod:`~aer.usage.effectiveness` -- joining usage to runs and verdicts, with the
  boundaries between recorded outcome classes intact;
* :mod:`~aer.usage.promotion` -- ``VERIFIED -> REUSED -> PROVEN``, with the thresholds
  as configuration and the source run excluded from counting as reuse;
* :mod:`~aer.usage.confidence` -- a deterministic, decomposable confidence, computed
  on demand and never written back.

Deliberately absent: any automatic inference of adoption from behaviour (section 22),
any propagation of usage into the training pipeline (sections 80-81), and any write to
the knowledge index (sections 4 and 45). This milestone accumulates evidence. Deciding
what to do with it is a later milestone's problem.
"""

from aer.usage.confidence import (
    DEFAULT_CONFIDENCE_WEIGHTS,
    DEFAULT_REUSE_SCALE,
    NO_FEEDBACK_PRIOR,
    ConfidenceWeights,
    ExperienceConfidence,
    ExperienceConfidenceService,
)
from aer.usage.effectiveness import (
    ExperienceEffectivenessReport,
    ExperienceEffectivenessService,
    classify_outcome,
)
from aer.usage.fingerprints import (
    CONTEXT_FINGERPRINT_LENGTH,
    QUERY_FINGERPRINT_LENGTH,
    RETRIEVAL_QUERY_MAX_LENGTH,
    context_fingerprint,
    normalize_text,
    query_fingerprint,
    sanitize_query,
)
from aer.usage.promotion import (
    DEFAULT_PROMOTION_POLICY,
    ExperiencePromotionService,
    PromotionDecision,
    PromotionPolicy,
)
from aer.usage.tracking import ExperienceUsageService, TrackedRetrievalResult

__all__ = [
    "CONTEXT_FINGERPRINT_LENGTH",
    "DEFAULT_CONFIDENCE_WEIGHTS",
    "DEFAULT_PROMOTION_POLICY",
    "DEFAULT_REUSE_SCALE",
    "NO_FEEDBACK_PRIOR",
    "QUERY_FINGERPRINT_LENGTH",
    "RETRIEVAL_QUERY_MAX_LENGTH",
    "ConfidenceWeights",
    "ExperienceConfidence",
    "ExperienceConfidenceService",
    "ExperienceEffectivenessReport",
    "ExperienceEffectivenessService",
    "ExperiencePromotionService",
    "ExperienceUsageService",
    "PromotionDecision",
    "PromotionPolicy",
    "TrackedRetrievalResult",
    "classify_outcome",
    "context_fingerprint",
    "normalize_text",
    "query_fingerprint",
    "sanitize_query",
]
