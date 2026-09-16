"""Human verifier: records a decision a person already made.

Minimal by design. There is no review queue, no notification, no user system and
no UI in this milestone -- those are product surfaces, and AER's job here is only
to make a human verdict *first-class* rather than something bolted on through
metadata.

Two deliberate choices:

* ``approved`` is ``bool | None``. ``None`` means "nobody has decided yet", which
  is a different situation from "a person rejected this". Treating the first as
  the second would manufacture a negative verdict out of silence, so an
  undecided human verifier raises
  :class:`~aer.exceptions.VerificationInputError` instead of returning
  ``passed=False``.
* ``reviewer`` is stored verbatim as an opaque business identifier. AER does not
  model identities; deciding what may legitimately be stored about a person is the
  caller's responsibility (round-4 brief, section 20).
"""

from __future__ import annotations

from aer.exceptions import VerificationInputError
from aer.runtime.enums import VerifierType
from aer.verification.base import VerificationContext, VerificationResult, VerifierBase


class HumanVerifier(VerifierBase):
    """Replays one human decision through the standard verification pipeline.

    ::

        review = HumanVerifier(
            approved=True,
            reviewer="ops-42",
            comment="heading reads correctly on staging",
        )
        run.verify(review, context=VerificationContext(run_id=run.run_id))

    ``required`` defaults to ``True``: a human who bothers to look is usually the
    authority on whether the task is done.
    """

    verifier_type = VerifierType.HUMAN

    def __init__(
        self,
        approved: bool | None,
        *,
        name: str = "human_review",
        reviewer: str | None = None,
        comment: str | None = None,
        required: bool = True,
    ) -> None:
        super().__init__(name, required=required)
        self._approved = approved
        self._reviewer = reviewer
        self._comment = comment

    @property
    def approved(self) -> bool | None:
        """The recorded decision, or ``None`` when nobody has decided yet."""
        return self._approved

    @property
    def reviewer(self) -> str | None:
        """Opaque identifier of the reviewer, stored verbatim."""
        return self._reviewer

    def verify(self, context: VerificationContext) -> VerificationResult:
        # The context is part of the contract but not part of this verifier's
        # evidence: a recorded decision is a fact about what a person concluded.
        del context

        if self._approved is None:
            raise VerificationInputError(
                f"Verifier {self.name!r} has no recorded human decision; "
                "an undecided review is not a rejection"
            )

        outcome = "approved" if self._approved else "rejected"
        reviewer = self._reviewer or "unnamed reviewer"
        return VerificationResult(
            passed=self._approved,
            message=self._comment or f"{outcome} by {reviewer}",
            result={"approved": self._approved, "reviewer": self._reviewer},
        )
