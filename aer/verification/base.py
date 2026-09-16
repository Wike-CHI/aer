"""The verifier interface and the value objects every verdict travels in.

A verifier is an **independent** check of what actually happened. Its whole
purpose is to be the piece of the system that is *not* the agent: the agent claims
the page was updated, the verifier observes that the response says 200 and the
document has exactly one ``<h1>``.

Three types live here, and nothing else does:

* :class:`VerificationContext` -- the typed input a verifier may look at. Not a
  bare ``dict``: an unconstrained mapping makes every verifier's contract
  invisible and lets a typo surface as a wrong verdict instead of an error.
* :class:`VerificationResult` -- the *one* shape every verifier returns. Different
  verifiers returning different shapes is how a verification system becomes
  unqueryable.
* :class:`Verifier` -- the protocol itself, plus :class:`VerifierBase` with the
  plumbing concrete verifiers share.

The module intentionally contains no verification logic. ``DETERMINISTIC``
primitives, environment checks, human decisions and LLM judges all implement the
same protocol and are interchangeable from the engine's point of view.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aer.exceptions import VerificationError, VerificationInputError
from aer.runtime.enums import VerifierType
from aer.runtime.serialization import JsonObject, JsonValue

_STRICT = ConfigDict(extra="forbid", validate_assignment=True)


class VerificationContext(BaseModel):
    """Everything a verifier is allowed to look at.

    Deliberately narrow. A verifier that wants to reach into the runtime, the
    database or the agent's memory is not verifying anything -- it is just
    repeating what it was told. Whatever the verifier needs to inspect (an HTTP
    status, a rendered document, an API response) arrives through ``payload``.

    ``artifacts`` exists so verdicts can later point at screenshots, saved HTML or
    logs; the Artifact milestone is not implemented yet, so it stays empty by
    default rather than being faked.
    """

    model_config = _STRICT

    run_id: str

    task: str | None = None
    """The task description, so a verifier (or an LLM judge) has the requirement."""

    artifacts: list[JsonObject] = Field(default_factory=list)
    """Artifact descriptors. Empty until the Artifact milestone lands."""

    payload: JsonObject = Field(default_factory=dict)
    """The evidence to check: observed values, documents, response bodies."""

    metadata: JsonObject = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """The uniform outcome of one verifier.

    ``passed`` is the fact. ``score`` is optional graded quality and is
    deliberately allowed to be ``None`` -- a deterministic yes/no check has no
    meaningful partial credit, and inventing ``1.0``/``0.0`` for it would make
    ``score`` a redundant restatement of ``passed`` that could later drift out of
    agreement with it. Nothing in AER ever derives ``passed`` from ``score``
    (round-4 brief, section 22); a verifier that wants a threshold has to apply it
    itself and say so.
    """

    model_config = _STRICT

    passed: bool

    score: float | None = Field(default=None, ge=0.0, le=1.0)
    message: str | None = None
    result: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)


@runtime_checkable
class Verifier(Protocol):
    """Structural interface for anything AER will run as a verifier.

    A protocol rather than an ABC so a caller can wrap an existing object or a
    third-party checker without importing AER's base class. The engine validates
    conformance before running anything, and validates the *return value* after.
    """

    name: str
    verifier_type: VerifierType
    required: bool

    def verify(self, context: VerificationContext) -> VerificationResult:
        """Return this verifier's verdict for ``context``."""
        ...


def as_verification_result(
    outcome: object,
    *,
    verifier: str,
    message: str | None = None,
) -> VerificationResult:
    """Normalise whatever a caller-supplied check returned into a verdict.

    Accepts a :class:`VerificationResult` (used as-is) or a plain ``bool`` (a
    predicate). Anything else is a programming error in the verifier, reported as
    such -- never coerced, because coercing it would produce a verdict nobody
    reasoned about.
    """
    if isinstance(outcome, VerificationResult):
        return outcome
    if isinstance(outcome, bool):
        # A failed predicate shows its fallback message; a passing one stays quiet.
        return VerificationResult(passed=outcome, message=None if outcome else message)
    raise VerificationError(
        f"Verifier {verifier!r} returned {type(outcome).__name__}; "
        "a verifier must return VerificationResult or bool"
    )


class VerifierBase:
    """Shared plumbing for concrete verifiers: identity, requirement flag, protocol.

    Subclasses must declare ``verifier_type`` and implement :meth:`verify`. The
    base class checks the declaration at construction time, so a missing
    ``verifier_type`` fails immediately rather than in the middle of a
    verification run.
    """

    verifier_type: VerifierType

    def __init__(self, name: str, *, required: bool = True) -> None:
        if not name:
            raise ValueError("A verifier must have a non-empty name")
        if getattr(self, "verifier_type", None) is None:
            raise TypeError(f"{type(self).__name__} must declare verifier_type")
        self._name = name
        self._required = required

    @property
    def name(self) -> str:
        """Stable identifier, recorded verbatim on every verdict."""
        return self._name

    @property
    def required(self) -> bool:
        """Whether a failure of this verifier blocks a verified success.

        Defaults to ``True``: an explicit check exists because somebody decided it
        mattered. Verifiers that only *grade* (an LLM quality score) should pass
        ``required=False`` so an opinion cannot veto a proven task.
        """
        return self._required

    def verify(self, context: VerificationContext) -> VerificationResult:
        """Return this verifier's verdict. Implemented by every subclass."""
        raise NotImplementedError(f"{type(self).__name__} does not implement verify()")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self._name!r}, "
            f"type={self.verifier_type.value}, required={self._required})"
        )


class CallableVerifier(VerifierBase):
    """A verifier whose logic is a caller-supplied callable.

    The point of this class is that AER never imports a vendor SDK to verify
    something. Whoever owns the check -- a Playwright script, an HTTP client, a
    model provider wrapper -- supplies a function; AER supplies the pipeline that
    records the answer. It is the shared implementation behind
    :class:`~aer.verification.deterministic.PredicateVerifier`,
    :class:`~aer.verification.environment.CallableEnvironmentVerifier` and
    :class:`~aer.verification.llm.LLMVerifier`.
    """

    def __init__(
        self,
        name: str,
        *,
        verifier_type: VerifierType,
        check: Callable[[VerificationContext], VerificationResult | bool],
        required: bool = True,
        message: str | None = None,
    ) -> None:
        # Set before super().__init__ so the explicit-type check in the base sees it.
        self.verifier_type = verifier_type
        super().__init__(name, required=required)
        self._check = check
        self._message = message

    def verify(self, context: VerificationContext) -> VerificationResult:
        return as_verification_result(
            self._check(context), verifier=self.name, message=self._message
        )


# ---------------------------------------------------------------------------
# Reading the typed context (never a bare `payload["key"]`)
# ---------------------------------------------------------------------------


def require_key(payload: JsonObject, key: str, *, verifier: str) -> JsonValue:
    """Fetch a required payload entry.

    Raises:
        VerificationInputError: the key is absent. Missing evidence is not
            negative evidence, so this never becomes ``passed=False``.
    """
    if key not in payload:
        raise VerificationInputError(
            f"Verifier {verifier!r} requires {key!r} in the verification payload"
        )
    return payload[key]


def require_int(payload: JsonObject, key: str, *, verifier: str) -> int:
    """Fetch a required integer payload entry.

    ``bool`` is rejected even though it is an ``int`` subclass: ``True`` arriving
    where a status code belongs is a bug in the caller, and quietly treating it as
    ``1`` would hide it.
    """
    value = require_key(payload, key, verifier=verifier)
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationInputError(
            f"Verifier {verifier!r} requires {key!r} to be an integer, got {type(value).__name__}"
        )
    return value


def require_str(payload: JsonObject, key: str, *, verifier: str) -> str:
    """Fetch a required string payload entry."""
    value = require_key(payload, key, verifier=verifier)
    if not isinstance(value, str):
        raise VerificationInputError(
            f"Verifier {verifier!r} requires {key!r} to be a string, got {type(value).__name__}"
        )
    return value


def require_bool(payload: JsonObject, key: str, *, verifier: str) -> bool:
    """Fetch a required boolean payload entry."""
    value = require_key(payload, key, verifier=verifier)
    if not isinstance(value, bool):
        raise VerificationInputError(
            f"Verifier {verifier!r} requires {key!r} to be a boolean, got {type(value).__name__}"
        )
    return value
