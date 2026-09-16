"""``ExperienceDistiller`` -- the seam between the pipeline and the provider.

Intentionally thin, and that thinness is the design. Everything a distillation pass
must do *regardless of who does the thinking* lives here:

1. call the provider with a complete evidence package (the provider never reads
   storage -- brief section 20);
2. refuse anything that is not an :class:`~aer.experience.candidate.ExperienceCandidate`,
   because a provider is arbitrary code and a malformed return value must not travel
   further;
3. normalise the candidate, so whitespace and blank entries cannot make two identical
   claims look different downstream.

It does not persist, does not decide the kind, and does not resolve the lifecycle.
Those belong to :class:`~aer.experience.service.ExperienceService` (brief section 26).

On the interface the brief specifies: TASKS.md Task 5.4 and brief section 20 sketch
``distill(run_id) -> ExperienceCandidate``. This takes a
:class:`~aer.experience.evidence.RunEvidence` instead, because building evidence
requires repositories and the distiller is explicitly forbidden from touching them.
The ``run_id`` entry point exists one level up, as
:meth:`ExperienceService.distill_run`, which is where a caller naturally starts.
"""

from __future__ import annotations

from aer.exceptions import DistillationError
from aer.experience.candidate import ExperienceCandidate, normalise_candidate
from aer.experience.evidence import RunEvidence
from aer.experience.provider import DistillationProvider


class ExperienceDistiller:
    """Runs one distillation pass and returns a normalised candidate."""

    def __init__(self, provider: DistillationProvider) -> None:
        if not isinstance(provider, DistillationProvider):
            raise DistillationError(
                f"{provider!r} does not implement the DistillationProvider protocol "
                "(needs a name and distill(evidence))"
            )
        self._provider = provider

    @property
    def provider(self) -> DistillationProvider:
        """The provider this distiller delegates to."""
        return self._provider

    @property
    def provider_name(self) -> str:
        """Provider identity, recorded on every experience it produces."""
        return self._provider.name

    def distill(self, evidence: RunEvidence) -> ExperienceCandidate:
        """Produce a candidate for ``evidence``.

        Raises:
            DistillationError: the provider returned something that is not a
                candidate. A provider that crashes raises its own exception and is
                reported by the service -- a broken provider is not a verdict about
                the run (the same distinction Milestone 4 drew for verifiers).
        """
        candidate = self._provider.distill(evidence)
        if not isinstance(candidate, ExperienceCandidate):
            raise DistillationError(
                f"Provider {self.provider_name!r} returned {type(candidate).__name__}; "
                "a distillation provider must return ExperienceCandidate"
            )
        return normalise_candidate(candidate)

    def __repr__(self) -> str:
        return f"ExperienceDistiller(provider={self.provider_name!r})"
