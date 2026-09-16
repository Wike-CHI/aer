"""Independent verification: the part of AER that disagrees with the agent.

The invariant this package exists to protect:

    an agent declaring a task complete, and the task actually being complete,
    are two different facts, and AER stores both.

So ``Run.status`` is never edited because a verifier disagreed. A run whose agent
said ``SUCCESS`` while an H1 count of 0 was observed keeps ``status = SUCCESS`` and
gains a failing verdict; the derived
:func:`~aer.verification.summary.is_verified_success` then reports ``False``. Only
that combination lets the Experience Distiller (Milestone 5) tell which "success
trajectories" must not be learned from.

Layout:

* :mod:`~aer.verification.base` -- the protocol and its value objects;
* :mod:`~aer.verification.deterministic` -- checks over values already obtained;
* :mod:`~aer.verification.environment` -- checks that go and look;
* :mod:`~aer.verification.human` / :mod:`~aer.verification.llm` -- the two lower
  trust levels, kept deliberately minimal and free of vendor or identity systems;
* :mod:`~aer.verification.engine` -- the single pipeline every verdict flows
  through;
* :mod:`~aer.verification.summary` -- aggregation and ``verified_success``.

Nothing here writes Experience, promotes workflows or trains anything. This
milestone produces the evidence a later one is allowed to trust.
"""

from aer.verification.base import (
    CallableVerifier,
    VerificationContext,
    VerificationResult,
    Verifier,
    VerifierBase,
    as_verification_result,
    require_bool,
    require_int,
    require_key,
    require_str,
)
from aer.verification.deterministic import (
    DeterministicVerifier,
    H1CountVerifier,
    HttpStatusVerifier,
    JsonValidVerifier,
    PredicateVerifier,
)
from aer.verification.engine import VerificationEngine
from aer.verification.environment import CallableEnvironmentVerifier
from aer.verification.human import HumanVerifier
from aer.verification.llm import LLMVerifier
from aer.verification.summary import VerificationSummary, is_verified_success

__all__ = [
    "CallableEnvironmentVerifier",
    "CallableVerifier",
    "DeterministicVerifier",
    "H1CountVerifier",
    "HttpStatusVerifier",
    "HumanVerifier",
    "JsonValidVerifier",
    "LLMVerifier",
    "PredicateVerifier",
    "VerificationContext",
    "VerificationEngine",
    "VerificationResult",
    "VerificationSummary",
    "Verifier",
    "VerifierBase",
    "as_verification_result",
    "is_verified_success",
    "require_bool",
    "require_int",
    "require_key",
    "require_str",
]
