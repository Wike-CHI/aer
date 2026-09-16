"""Where distillation intelligence comes from -- and why AER ships none of it.

The provider is the only pluggable part of the experience pipeline, and it is
deliberately vendor-free (brief sections 24-25). AER imports no model SDK, reads no
API key and knows no provider's request shape. Whoever owns the prompt supplies a
callable that turns evidence into a candidate.

That is not just portability. It is what makes the whole pipeline testable offline:
every architectural guarantee this milestone claims -- kinds decided by facts, no
half-written experiences, idempotency, provenance merging -- is verified with a
provider that is a Python function. If a test needed a network round trip to assert
them, none of them would be reliably assertable (brief section 25).

A real provider is a thin adapter::

    class MyModelProvider:
        name = "gpt-4o-mini"

        def distill(self, evidence: RunEvidence) -> ExperienceCandidate:
            reply = my_client.complete(prompt_for(evidence))
            return ExperienceCandidate(**json.loads(reply))

Note what such an adapter does **not** decide: the kind stored, the confidence, the
lifecycle status or whether the claim is verified. Those belong to the system
(``docs/DECISIONS.md`` D-031).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from aer.experience.candidate import ExperienceCandidate
from aer.experience.evidence import RunEvidence


@runtime_checkable
class DistillationProvider(Protocol):
    """Structural interface for anything that can propose an experience.

    ``name`` is part of the contract rather than an optional nicety: every
    experience records which provider produced it, and provenance that can be
    omitted ends up being omitted.
    """

    name: str

    def distill(self, evidence: RunEvidence) -> ExperienceCandidate:
        """Propose an experience for ``evidence``.

        Implementations may perform I/O and may raise; the pipeline records a crash
        and re-raises rather than inventing a result (brief section 43).
        """
        ...


class CallableDistillationProvider:
    """Adapts a plain function into a provider.

    The shipped implementation, and the reason tests need no model::

        provider = CallableDistillationProvider(
            name="rule-based",
            func=lambda evidence: ExperienceCandidate(
                domain="wordpress", title="...", problem="...",
            ),
        )

    Also the seam for a prompt-engineering workflow: the function is where a real
    prompt would live, and it can be swapped without touching the pipeline.
    """

    def __init__(
        self,
        func: Callable[[RunEvidence], ExperienceCandidate],
        *,
        name: str = "callable",
    ) -> None:
        if not name:
            raise ValueError("A distillation provider must have a non-empty name")
        self._func = func
        self.name = name

    def distill(self, evidence: RunEvidence) -> ExperienceCandidate:
        """Run the wrapped function."""
        return self._func(evidence)

    def __repr__(self) -> str:
        return f"CallableDistillationProvider(name={self.name!r})"
