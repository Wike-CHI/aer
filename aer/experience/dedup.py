"""Deterministic duplicate detection -- and the deliberate refusal to be clever.

The problem (agent.md #25, brief sections 39-41): the same problem recurs, and the
store must not accumulate ten experiences that say the same thing. The right answer
is to attach the new run to the existing experience.

The mechanism is a *fingerprint*: the canonical form of ``(kind, domain, title,
problem)``, hashed into a fixed-length key that is stored and indexed. Identical
fingerprints are the same claim; everything else is treated as different.

Normalisation is intentionally shallow:

======================  ==========================================================
applied                 why
======================  ==========================================================
``NFKC``                folds full-width characters and compatibility forms, so text
                        pasted out of a browser or a CJK IME compares equal
``casefold``            ``WordPress`` and ``wordpress`` are the same problem
whitespace collapsing   line breaks and double spaces are formatting, not meaning
======================  ==========================================================

**Not applied: punctuation stripping, stemming, synonym folding, embeddings.** Each
of those can merge two genuinely different problems, and the failure mode is
asymmetric: a missed merge costs one extra row, while a wrong merge silently attaches
evidence to an unrelated claim and makes the store *worse* than no store. Similarity
that is not an exact fingerprint match is left for the retrieval milestone, where a
human-visible ranking can carry the ambiguity instead of a silent write (D-035).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from aer.runtime.enums import ExperienceKind

#: Any run of Unicode whitespace.
_WHITESPACE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    """Canonical form of one text field, used for fingerprinting only."""
    folded = unicodedata.normalize("NFKC", text)
    return _WHITESPACE.sub(" ", folded).strip().casefold()


def dedup_key_for(
    *,
    kind: ExperienceKind,
    domain: str,
    title: str,
    problem: str,
) -> str:
    """Fingerprint of a claim, stable across runs and providers.

    ``kind`` is part of the key on purpose: "this approach failed" and "this approach
    worked" are different claims about the same words, and merging them would delete
    the distinction the whole milestone is built on.

    Returns:
        A hex digest. Hashed rather than concatenated so the column is a predictable
        size and indexable; the source fields remain on the row, so the key itself
        never has to be read by a human.
    """
    canonical = "\u241f".join(
        (
            ExperienceKind(kind).value,
            normalise_text(domain),
            normalise_text(title),
            normalise_text(problem),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
