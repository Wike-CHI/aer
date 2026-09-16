"""Deterministic verifiers: same input, same verdict, no model, no network.

These are the highest-trust verifiers in AER (``VerifierType.DETERMINISTIC``) and
the cheapest to reason about, so the interesting business checks should be pushed
here whenever possible. An LLM judge disagrees with itself across runs; code does
not.

Scope note (round-4 brief, section 45): these are *primitives*, not an SEO
auditor. ``HttpStatusVerifier`` does not make a request -- it judges a status code
that was already obtained, which keeps the "what is true" part separable from the
"I/O that fetched it" part, and makes the check testable without a server.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from aer.runtime.enums import VerifierType
from aer.runtime.serialization import JsonValue
from aer.verification.base import (
    VerificationContext,
    VerificationResult,
    VerifierBase,
    as_verification_result,
    require_int,
    require_str,
)


class DeterministicVerifier(VerifierBase):
    """Base for verifiers that are pure functions of the context.

    Every subclass must be reproducible: no clocks, no randomness, no network. If
    a check needs the outside world it is an
    :class:`~aer.verification.environment.CallableEnvironmentVerifier` instead, and
    saying so is the point of having two types.
    """

    verifier_type = VerifierType.DETERMINISTIC


class PredicateVerifier(DeterministicVerifier):
    """Turns an arbitrary boolean condition into a standard verdict.

    The escape hatch for checks that do not deserve a class::

        H1_COUNT = PredicateVerifier(
            name="h1_count",
            predicate=lambda ctx: ctx.payload.get("h1_count") == 1,
            message="the page must render exactly one <h1>",
        )

    The predicate returns ``bool`` (or a full :class:`VerificationResult` when it
    wants to attach detail), so an ``assert``-style condition becomes a recorded,
    queryable verdict.
    """

    def __init__(
        self,
        *,
        name: str,
        predicate: Callable[[VerificationContext], VerificationResult | bool],
        required: bool = True,
        message: str | None = None,
    ) -> None:
        super().__init__(name, required=required)
        self._predicate = predicate
        self._message = message

    @property
    def predicate(self) -> Callable[[VerificationContext], VerificationResult | bool]:
        """The condition this verifier evaluates."""
        return self._predicate

    def verify(self, context: VerificationContext) -> VerificationResult:
        return as_verification_result(
            self._predicate(context), verifier=self.name, message=self._message
        )


class HttpStatusVerifier(DeterministicVerifier):
    """Compare an already-obtained HTTP status code against the expected one.

    ::

        verifier = HttpStatusVerifier(expected_status=200)
        run.verify(verifier, context=VerificationContext(
            run_id=run.run_id, payload={"actual_status": 200},
        ))

    No request is issued here: whatever performed the call is responsible for
    handing over the status it observed.
    """

    def __init__(
        self,
        expected_status: int,
        *,
        name: str = "http_status",
        payload_key: str = "actual_status",
        required: bool = True,
    ) -> None:
        super().__init__(name, required=required)
        self._expected_status = expected_status
        self._payload_key = payload_key

    @property
    def expected_status(self) -> int:
        """The status code the check requires."""
        return self._expected_status

    @property
    def payload_key(self) -> str:
        """Payload entry holding the observed status code."""
        return self._payload_key

    def verify(self, context: VerificationContext) -> VerificationResult:
        actual = require_int(context.payload, self._payload_key, verifier=self.name)
        passed = actual == self._expected_status
        return VerificationResult(
            passed=passed,
            message=(
                f"HTTP {actual} matches the expected {self._expected_status}"
                if passed
                else f"HTTP {actual} does not match the expected {self._expected_status}"
            ),
            result={
                "actual_status": actual,
                "expected_status": self._expected_status,
            },
        )


class H1CountVerifier(DeterministicVerifier):
    """Check how many ``<h1>`` elements a page actually renders.

    The first real WordPress check: an agent that "updated the heading" but left
    zero ``<h1>`` tags has not satisfied the requirement, however confidently it
    reports success.
    """

    def __init__(
        self,
        expected_count: int,
        *,
        name: str = "h1_count",
        payload_key: str = "actual_count",
        required: bool = True,
    ) -> None:
        super().__init__(name, required=required)
        self._expected_count = expected_count
        self._payload_key = payload_key

    @property
    def expected_count(self) -> int:
        """The number of ``<h1>`` elements the check requires."""
        return self._expected_count

    @property
    def payload_key(self) -> str:
        """Payload entry holding the observed count."""
        return self._payload_key

    def verify(self, context: VerificationContext) -> VerificationResult:
        actual = require_int(context.payload, self._payload_key, verifier=self.name)
        passed = actual == self._expected_count
        return VerificationResult(
            passed=passed,
            message=(f"found {actual} <h1> element(s), expected {self._expected_count}"),
            result={"actual_count": actual, "expected_count": self._expected_count},
        )


class JsonValidVerifier(DeterministicVerifier):
    """Check that a payload entry parses as JSON.

    Deliberately *not* a JSON-Schema engine: full schema validation would pull in a
    heavyweight dependency, and the brief is explicit that this milestone is about
    the verification architecture rather than a schema platform. A caller who needs
    structural validation writes a predicate.
    """

    def __init__(
        self,
        *,
        name: str = "json_valid",
        payload_key: str = "document",
        required: bool = True,
    ) -> None:
        super().__init__(name, required=required)
        self._payload_key = payload_key

    @property
    def payload_key(self) -> str:
        """Payload entry holding the document to parse."""
        return self._payload_key

    def verify(self, context: VerificationContext) -> VerificationResult:
        document = require_str(context.payload, self._payload_key, verifier=self.name)
        try:
            parsed: JsonValue = json.loads(document)
        except json.JSONDecodeError as exc:
            # A malformed document is a *verdict* (passed=False), not a crash: the
            # verifier ran fine and observed something that is not JSON.
            return VerificationResult(
                passed=False,
                message=f"the document is not valid JSON: {exc.msg} at line {exc.lineno}",
                result={"valid": False, "error": f"{exc.msg} at line {exc.lineno}"},
            )
        return VerificationResult(
            passed=True,
            message="the document parses as JSON",
            result={"valid": True, "top_level_type": type(parsed).__name__},
        )
