"""AER exception hierarchy.

Every error raised by AER derives from :class:`AERError` so callers can catch the
whole library with a single ``except`` clause when they do not care about the
distinction.

Rule (agent.md #50): never collapse everything into a bare ``Exception`` and never
swallow a failure silently. Infrastructure failures keep their original cause via
``raise ... from exc`` at the point where they are translated.

.. note::
   This module was named ``aer.errors`` until Milestone 3. It was renamed because
   ``AER.errors`` is now the public handle for the :class:`ErrorRepository`, and
   having ``aer.errors`` (module) and ``aer.errors`` (repository accessor) mean two
   different things was a permanent readability trap. See ``docs/DECISIONS.md`` D-011.
"""

from __future__ import annotations


class AERError(Exception):
    """Base class for every error raised by AER."""


class ConfigurationError(AERError):
    """Deployment configuration is missing, malformed or self-contradictory.

    Raised instead of silently falling back to a default: a wrong data directory
    in a container is indistinguishable from data loss, so an unreadable
    environment variable must fail loudly at startup rather than write to the
    wrong place (see ``docs/DECISIONS.md`` D-046).
    """


class StorageError(AERError):
    """A persistence operation failed.

    Wraps the underlying driver/ORM failure while preserving it as
    ``__cause__``, so the original message and traceback remain available.
    """


class RecordNotFoundError(StorageError):
    """A record that the caller expected to exist was not found."""


class RunStateError(AERError):
    """An illegal Run state transition was attempted.

    Examples: finishing an already finished run, emitting an event after the run
    reached a terminal status, or opening a hook on a finished run.
    """


class HookStateError(AERError):
    """A hook context manager was used illegally.

    Examples: re-entering a :class:`~aer.runtime.hooks.ToolContext` that has
    already been closed, or nesting the same context object twice.
    """


class VerificationError(AERError):
    """A verification *attempt* could not produce a verdict.

    This is deliberately separate from "the verification failed". A verdict of
    ``passed=False`` is a **result**: the verifier ran successfully and observed
    that reality does not satisfy the requirement. This exception means the
    verifier itself broke -- a missing input, a non-JSON-safe payload, a judge
    callable returning garbage. Recording a crash as a failed verification would
    be a lie about the world, so crashes are recorded as ``ERROR`` events instead
    and the exception is re-raised (round-4 brief, sections 14 and 40).
    """


class VerificationInputError(VerificationError):
    """A verifier could not obtain the input it needs from the context.

    Raised when a required payload key is absent or has the wrong type. Missing
    evidence is not negative evidence, so this never degrades into
    ``passed=False``.
    """


class ExperienceError(AERError):
    """Base class for Experience-layer failures."""


class ExperienceLifecycleError(ExperienceError):
    """An illegal Experience status transition was attempted.

    Raised for jumping over a stage (``RAW`` -> ``PROVEN``), re-entering the status
    an Experience is already in, or leaving a terminal status. The lifecycle is
    deterministic code; a caller that wants a different status has to change the
    transition table, not assign around it.
    """


class DistillationError(ExperienceError):
    """The distillation pipeline could not produce an Experience.

    Covers "no provider is configured", "the provider crashed" and "the provider
    returned something unusable". In every case **nothing is persisted** -- a
    failed distillation never leaves a half-written Experience, and never fabricates
    a ``FAILURE`` one to explain the failure (round-5 brief, section 43).
    """


class CandidateValidationError(DistillationError):
    """A distilled candidate was structurally unusable.

    Raised when the claim has no identity -- empty domain, title or problem -- or
    when its list fields contain blank entries. Distinct from "the candidate
    describes a failure": a failure is a perfectly valid thing to record.
    """


class KnowledgeError(AERError):
    """Base class for Knowledge-plane failures (Milestone 6).

    The knowledge plane is a **projection** of the experience store, never a
    second source of truth, so its failures are deliberately a separate family
    from :class:`StorageError`: a broken knowledge index says nothing about the
    durability of the runs and experiences it was built from.
    """


class KnowledgeIndexUnavailable(KnowledgeError):
    """The knowledge index could not be reached or opened.

    Raised instead of returning an empty result. "There is no relevant
    experience" and "the knowledge index is broken" are different facts, and an
    agent that cannot tell them apart will confidently proceed as if it had
    checked. Callers that want to degrade gracefully must catch this explicitly
    (round-6 brief, section 80).
    """


class KnowledgeQueryError(KnowledgeError):
    """A retrieval request was malformed.

    Raised for an empty query, an unknown mode or a mismatch between the policy
    and the requested filters -- anything where guessing a correction would
    silently answer a different question than the caller asked.
    """


class ProjectionError(KnowledgeError):
    """Writing an experience into the knowledge index failed.

    Critically, this never implies the SQLite write failed or should be
    undone: the experience is already committed and is the durable fact. A
    raised :class:`ProjectionError` means only that the projection is now stale
    and a rebuild is due (round-6 brief, sections 56 and 57).
    """


class KnowledgeSchemaError(KnowledgeError):
    """The knowledge index on disk is not the shape this build expects.

    Carries no repair strategy on purpose: the knowledge database is
    rebuildable from SQLite, so the answer to a schema mismatch is *rebuild*,
    not migrate (round-6 brief, section 77).
    """
