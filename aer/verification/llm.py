"""LLM verifier: the weakest independent evidence, kept vendor-neutral.

A model judging a model's work is the *lowest* trust level AER accepts
(``VerifierType.LLM``, declared last in the enum). It is still much better than the
agent judging itself -- which AER refuses to accept at all, and which is why
``AGENT_SELF`` is not even in the vocabulary.

Two design constraints from the brief (section 21) are enforced here:

* **No vendor binding.** AER imports no model SDK and reads no API key. The caller
  supplies a ``judge`` callable -- typically a thin wrapper that calls whatever
  provider they use, with their own prompt, timeout and retry policy.
* **Optional by default.** ``required=False``. An LLM opinion is a quality signal,
  not a gate: letting a flaky judge veto a task whose deterministic checks all
  passed would replace one model's guesswork for another's. Pass
  ``required=True`` when a judge genuinely is the acceptance criterion, and the
  decision is then explicit and visible in the data.
"""

from __future__ import annotations

from collections.abc import Callable

from aer.runtime.enums import VerifierType
from aer.verification.base import (
    CallableVerifier,
    VerificationContext,
    VerificationResult,
)


class LLMVerifier(CallableVerifier):
    """Runs a caller-supplied judge function and records its verdict.

    ::

        def judge(context: VerificationContext) -> VerificationResult:
            answer = my_provider.complete(prompt_for(context))
            return VerificationResult(
                passed=answer.get("satisfied") is True,
                score=float(answer["score"]),
                message=str(answer["reasoning"]),
            )

        run.verify(LLMVerifier(name="fluency", judge=judge))
    """

    def __init__(
        self,
        *,
        name: str = "llm_judge",
        judge: Callable[[VerificationContext], VerificationResult | bool],
        required: bool = False,
        message: str | None = None,
    ) -> None:
        super().__init__(
            name,
            verifier_type=VerifierType.LLM,
            check=judge,
            required=required,
            message=message,
        )
        self._judge = judge

    @property
    def judge(self) -> Callable[[VerificationContext], VerificationResult | bool]:
        """The caller-supplied judgement function."""
        return self._judge
