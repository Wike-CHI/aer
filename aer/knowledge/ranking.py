"""Scoring: turning a raw BM25 number into a defensible ordering.

Four signals, one number, and one property that matters more than the exact
weights:

    a trust bonus can reorder *similar* matches and can never overturn a clear
    relevance gap.

That is enforced by the shape of the formula rather than by tuning. The text
score is a *multiplicative gate* -- ``text_score * (1 + bonuses)`` -- and the
bonuses sum to at most :data:`MAX_BONUS`, so a candidate whose text score is
better by more than a factor of ``1 + MAX_BONUS`` wins regardless of how trusted,
on-domain or fresh the other one is. An additive formula would let a trusted
irrelevant answer outrank a relevant unverified one, which is exactly the failure
this milestone exists to avoid.

Two deliberate omissions:

* **``confidence`` is not an input.** It is a constant ``0.0`` in this milestone
  (D-037), so feeding it in would either do nothing or -- worse -- invite someone
  to "fix" retrieval by making up numbers in a field nothing calibrates.
* **``source_count`` is not an input either.** It is real evidence and it is
  reported, but section 44 is explicit that more runs is not a correctness rate:
  three runs that all did the wrong thing are still wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from aer.runtime.enums import ExperienceStatus

__all__ = [
    "BM25_BETTER_IS_LOWER",
    "DOMAIN_BONUS_WEIGHT",
    "FRESHNESS_BONUS_WEIGHT",
    "FRESHNESS_HALF_LIFE_DAYS",
    "MAX_BONUS",
    "MAX_BONUS_FACTOR",
    "STATUS_TRUST",
    "TRUST_BONUS_WEIGHT",
    "combine",
    "domain_score",
    "freshness_score",
    "text_score",
    "trust_score",
]

#: Which way NeuG's ``bm25()`` points, established by running it rather than by
#: trusting the docs -- the FTS page says "smaller is more relevant" but does not
#: say the scores are *negative*, which they are: a strong match comes back around
#: ``-2.0`` and a weak one around ``-0.5``, and ``ORDER BY score ASC`` puts the
#: strong one first.
#:
#: A one-line switch, because the day the engine changes convention this is the
#: single place that has to change, and every other comparison in AER can keep
#: assuming "bigger is better".
BM25_BETTER_IS_LOWER = True

#: How much a fully-trusted outcome can lift a candidate, as a fraction of its
#: text score.
TRUST_BONUS_WEIGHT = 0.35
#: How much an exact domain match can lift a candidate.
DOMAIN_BONUS_WEIGHT = 0.20
#: How much a brand-new experience can lift a candidate.
FRESHNESS_BONUS_WEIGHT = 0.10

#: Upper bound of the combined bonus. Published because the guarantee in the module
#: docstring is stated in terms of it: ``text_a > text_b * MAX_BONUS_FACTOR`` wins.
MAX_BONUS = TRUST_BONUS_WEIGHT + DOMAIN_BONUS_WEIGHT + FRESHNESS_BONUS_WEIGHT
#: The largest multiplier any set of bonuses can produce.
MAX_BONUS_FACTOR = 1.0 + MAX_BONUS

#: Days after which freshness decays to one half.
FRESHNESS_HALF_LIFE_DAYS = 180.0

#: How much each lifecycle status contributes to trust.
#:
#: Every status at or above ``VERIFIED`` scores the same. That is not laziness: the
#: differences between ``VERIFIED``, ``REUSED`` and ``PROVEN`` are supposed to come
#: from *usage* data, which does not exist yet, and inventing a gradient now would
#: bake a guess into the ranking that later has to be un-learned.
STATUS_TRUST: Mapping[ExperienceStatus, float] = {
    ExperienceStatus.RAW: 0.10,
    ExperienceStatus.DISTILLED: 0.40,
    ExperienceStatus.VERIFIED: 1.0,
    ExperienceStatus.REUSED: 1.0,
    ExperienceStatus.PROVEN: 1.0,
    ExperienceStatus.TRAINING_CANDIDATE: 1.0,
    ExperienceStatus.TRAINING_DATA: 1.0,
    # Deprecated knowledge is understood to be wrong. It only reaches the ranker
    # when a caller asked for it by name, and then it belongs at the bottom.
    ExperienceStatus.DEPRECATED: 0.0,
}

#: Credit an outcome that has not been independently confirmed.
UNVERIFIED_OUTCOME_FACTOR = 0.5


def text_score(bm25: float) -> float:
    """Map NeuG's BM25 into a score where **higher is better**, in ``[0, 1)``.

    Two facts about the engine's output, both measured against neug 0.2.0 rather
    than inferred: it points the opposite way to every other score in AER, and its
    values are **negative**. A strong match returns roughly ``-2.0``; a document
    that shares a single term returns roughly ``-0.5``.

    That makes the obvious transformation wrong. ``1 / (1 + bm25)`` -- the natural
    "turn a distance into a similarity" move -- maps ``-2.0`` to ``-1`` and
    ``-0.5`` to ``2``, inverting the ranking. And clamping the negative input to
    zero before the reciprocal, which is the defensive-looking version, collapses
    *every* relevance to the same value.

    So the direction is corrected first (:data:`BM25_BETTER_IS_LOWER`), then the
    magnitude is squashed with ``r / (1 + r)``: strictly increasing in relevance,
    bounded, and zero exactly when there is no relevance at all.

    The clamp after the sign flip is not defensive padding. If the engine ever
    returns a *positive* score meaning "relevant", the corrected value would be
    negative and would silently rank the best match last; clamping makes that
    mistake visible as a uniform ranking instead of an inverted one.
    """
    relevance = -bm25 if BM25_BETTER_IS_LOWER else bm25
    relevance = max(relevance, 0.0)
    return relevance / (1.0 + relevance)


def trust_score(status: ExperienceStatus, *, outcome_verified: bool) -> float:
    """How much this experience's own record vouches for it, in ``[0, 1]``.

    ``outcome_verified`` halves the credit rather than zeroing it: an unverified
    entry still describes something a real run did, it just has not been confirmed
    by anything outside the agent. Zeroing it would make the distinction
    unrecoverable later.
    """
    base = STATUS_TRUST.get(ExperienceStatus(status), 0.0)
    return base if outcome_verified else base * UNVERIFIED_OUTCOME_FACTOR


def domain_score(hit_domain: str, query_domain: str | None) -> float:
    """``1.0`` for an exact domain match, ``0.0`` otherwise.

    A caller that did not name a domain gets no domain signal at all -- not a
    penalty for every candidate, and not a bonus for some arbitrary subset. The
    alternative (fuzzy domain matching, parent domains, taxonomies) needs query
    evidence this milestone does not have.
    """
    if query_domain is None:
        return 0.0
    return 1.0 if hit_domain == query_domain else 0.0


def freshness_score(updated_at: datetime, *, now: datetime) -> float:
    """Exponential decay with a half-life of :data:`FRESHNESS_HALF_LIFE_DAYS`.

    Deliberately weak and deliberately slow. Guidance about WordPress 403s does not
    rot the way a stock price does, and a ranking that chases recency would discard
    the hard-won recovery experiences this system exists to accumulate. This is a
    tie-breaker, not a filter: nothing is ever excluded for being old.
    """
    age_days = (now - updated_at).total_seconds() / 86_400.0
    if age_days <= 0.0:
        return 1.0
    return 0.5 ** (age_days / FRESHNESS_HALF_LIFE_DAYS)


def combine(
    *,
    text: float,
    trust: float,
    domain: float,
    freshness: float,
) -> float:
    """Final retrieval score: the text score, lifted by the supporting signals.

    See the module docstring for the guarantee this shape buys and why an additive
    blend was rejected. The result is unbounded above only in the sense that it can
    reach ``text * MAX_BONUS_FACTOR``, which is still ``<= 1.65``.
    """
    bonus = (
        TRUST_BONUS_WEIGHT * trust
        + DOMAIN_BONUS_WEIGHT * domain
        + FRESHNESS_BONUS_WEIGHT * freshness
    )
    return text * (1.0 + bonus)
