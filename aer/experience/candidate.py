"""``ExperienceCandidate`` -- what a distiller hands back, before anything is stored.

A candidate is an *unverified proposal*, never a record (brief section 23). It has no
id, no status and no place in the lifecycle: the application layer decides whether it
becomes an :class:`~aer.runtime.models.Experience`, where it enters the lifecycle and
what the system -- not the provider -- says about it.

Two fields exist purely to keep the boundary honest:

* ``kind`` is the provider's *opinion* of what kind of experience this is. It is
  never authoritative (section 27); the system classifies from recorded facts, and a
  disagreement is recorded in metadata so the override is auditable (section 54);
* ``confidence_hint`` is the provider's self-assessment. It must not become the
  experience's ``confidence`` (section 38). It is preserved as a hint for the
  milestone that can actually calibrate it against reuse data.

Normalisation is not cosmetic here: blank list entries, ``""`` vs ``None`` for absent
text and stray whitespace are exactly the shapes a model emits, and letting them into
storage would make two identical claims compare as different.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aer.exceptions import CandidateValidationError
from aer.runtime.enums import ExperienceKind
from aer.runtime.serialization import JsonObject

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class ExperienceCandidate(BaseModel):
    """A distilled, not-yet-stored experience claim."""

    model_config = _FROZEN

    domain: str
    title: str
    problem: str

    kind: ExperienceKind | None = None
    """The provider's suggestion. The system decides the stored kind."""

    symptoms: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    """What was tried and did not work. Required in spirit for ``RECOVERY``."""

    root_cause: str | None = None
    """A hypothesis. For an unresolved failure it is usually absent, and that is
    correct -- see brief section 31."""

    root_cause_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    solution: str | None = None
    """``None`` is a legitimate value for an unresolved failure."""

    recommended_workflow: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    generalizable: bool = True
    confidence_hint: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: JsonObject = Field(default_factory=dict)

    def __repr__(self) -> str:
        kind = self.kind.value if self.kind is not None else "-"
        return f"ExperienceCandidate(domain={self.domain!r}, kind={kind}, title={self.title!r})"


def _clean(value: str | None) -> str | None:
    """Strip surrounding whitespace; turn a blank string into ``None``.

    "No solution" and "a solution consisting of a space" must not be two different
    things in the store.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _clean_items(items: tuple[str, ...]) -> tuple[str, ...]:
    """Strip each entry, drop blanks, and drop duplicates while keeping order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return tuple(cleaned)


def normalise_candidate(candidate: ExperienceCandidate) -> ExperienceCandidate:
    """Return a cleaned copy of ``candidate``.

    Applied before validation and before the dedup key is computed, so that two
    providers describing the same claim with different whitespace produce the same
    fingerprint.
    """
    return ExperienceCandidate(
        domain=candidate.domain.strip(),
        title=candidate.title.strip(),
        problem=candidate.problem.strip(),
        kind=candidate.kind,
        symptoms=_clean_items(candidate.symptoms),
        failed_attempts=_clean_items(candidate.failed_attempts),
        root_cause=_clean(candidate.root_cause),
        root_cause_confidence=candidate.root_cause_confidence,
        solution=_clean(candidate.solution),
        recommended_workflow=_clean_items(candidate.recommended_workflow),
        avoid=_clean_items(candidate.avoid),
        generalizable=candidate.generalizable,
        confidence_hint=candidate.confidence_hint,
        metadata=dict(candidate.metadata),
    )


def validate_candidate(candidate: ExperienceCandidate) -> None:
    """Reject a candidate that cannot become a usable experience.

    Only *identity* is checked. A candidate is refused when there is nothing to
    store -- no domain, no title, no problem -- because an experience that cannot be
    found again is not knowledge.

    Deliberately **not** checked:

    * ``root_cause`` / ``solution`` -- absence is meaningful for a failure, and
      demanding them here would push a provider into inventing one (sections 6, 31);
    * ``kind`` -- the provider has no say (section 27);
    * prose quality -- that is a prompt problem, not an architectural one, and this
      milestone is about the architecture (section 25).

    Raises:
        CandidateValidationError: the claim has no identity.
    """
    missing = [
        name for name in ("domain", "title", "problem") if not getattr(candidate, name).strip()
    ]
    if missing:
        raise CandidateValidationError(
            "Distilled candidate has no identity: "
            + ", ".join(f"{name} is empty" for name in missing)
        )
