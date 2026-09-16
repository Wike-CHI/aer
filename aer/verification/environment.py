"""Environment verifier: checks the real world, not the agent's description of it.

The distinction from :mod:`aer.verification.deterministic` is a difference in
*kind*, not in strictness:

* ``DETERMINISTIC`` judges values it was handed (``200 == 200``);
* ``ENVIRONMENT`` goes and looks (fetch the live page, stat the file, query the
  WordPress REST API).

Both can be fully trustworthy, but only the second one can be affected by the
world changing between the agent's action and the check -- which is exactly the
failure mode a post-hoc verifier exists to catch. Keeping them as separate
:class:`~aer.runtime.enums.VerifierType` values means a query like "was this run
verified against reality, or only against its own report?" is answerable from the
data.
"""

from __future__ import annotations

from collections.abc import Callable

from aer.runtime.enums import VerifierType
from aer.verification.base import (
    CallableVerifier,
    VerificationContext,
    VerificationResult,
)


class CallableEnvironmentVerifier(CallableVerifier):
    """Run a caller-supplied function that inspects the external environment.

    ::

        def wordpress_h1_is_present() -> VerificationResult:
            page = requests.get(url, timeout=10)
            return VerificationResult(
                passed=page.status_code == 200 and page.text.count("<h1") == 1,
                result={"status": page.status_code},
            )

        verifier = CallableEnvironmentVerifier(
            name="live_h1",
            check=lambda ctx: wordpress_h1_is_present(),
        )
        run.verify(verifier)

    AER does not perform the I/O itself. That keeps HTTP clients, credentials and
    retry policy out of the runtime, and keeps this verifier testable by passing a
    different callable.
    """

    def __init__(
        self,
        *,
        name: str,
        check: Callable[[VerificationContext], VerificationResult | bool],
        required: bool = True,
        message: str | None = None,
    ) -> None:
        super().__init__(
            name,
            verifier_type=VerifierType.ENVIRONMENT,
            check=check,
            required=required,
            message=message,
        )
