"""The knowledge plane: a searchable projection of the experience store.

Two sentences carry the whole design, and both are worth repeating because getting
either wrong is how this milestone fails:

    **SQLite is the source of truth. NeuG is a rebuildable projection of it.**

Nothing here writes to the experience store, nothing here is the only copy of
anything, and everything here can be produced again from ``experiences`` and
``experience_sources`` alone. The knowledge database is disposable by
construction -- deleting it costs a rebuild, not data.

What the package contains, and why each part is where it is:

* :mod:`~aer.knowledge.base` -- the :class:`~aer.knowledge.base.KnowledgeIndex`
  protocol. The runtime and experience layers never see NeuG.
* :mod:`~aer.knowledge.schema` -- projection schema V1 and its version number.
  Versioned by a number rather than migrated: the index is rebuildable, so a shape
  change means "rebuild", not "write a migration for a copy".
* :mod:`~aer.knowledge.query` -- caller text into a full-text expression. Necessary
  because the engine parses BM25 queries as FTS5 syntax, where ``wp-json 404`` is
  an error rather than a search.
* :mod:`~aer.knowledge.neug` -- the only module that imports the engine. Deferred,
  so a platform without a NeuG wheel can still use everything else.
* :mod:`~aer.knowledge.projector` -- the one-way copy, the drift check and the
  staged rebuild.
* :mod:`~aer.knowledge.retriever` / :mod:`~aer.knowledge.ranking` /
  :mod:`~aer.knowledge.formatter` -- what may be returned, in what order, and how it
  is worded.
* :mod:`~aer.knowledge.status` / :mod:`~aer.knowledge.cli` -- the operator surface.

NeuG itself is not imported here. Retrieval on a machine without the engine raises
:class:`~aer.exceptions.KnowledgeIndexUnavailable` at the point of use, which is
the only place where the answer is actually "no".
"""

from aer.knowledge.base import (
    IndexedExperience,
    IndexedRunRef,
    IndexFilters,
    IndexMatch,
    IndexQuery,
    KnowledgeIndex,
)
from aer.knowledge.formatter import (
    DEFAULT_MAX_CHARS,
    PREAMBLE,
    ExperienceContextFormatter,
)
from aer.knowledge.models import (
    DEFAULT_RETRIEVAL_LIMIT,
    KNOWN_FAILURE_LABEL,
    MAX_RETRIEVAL_LIMIT,
    MIN_RETRIEVAL_LIMIT,
    UNVERIFIED_STATUS_ORDER,
    VERIFIED_STATUS_ORDER,
    VERIFIED_STATUSES,
    ExperienceSearchQuery,
    RetrievalHit,
    RetrievalResult,
)
from aer.knowledge.projector import (
    DEFAULT_BATCH_SIZE,
    DriftReport,
    KnowledgeProjector,
    ProjectionOutcome,
    RebuildReport,
)
from aer.knowledge.query import fts_expression, query_terms
from aer.knowledge.retriever import ExperienceRetriever, RetrievalPolicy
from aer.knowledge.schema import (
    EXPERIENCE_INDEXED_PROPERTIES,
    FTS_PROPERTY_WEIGHTS,
    PROJECTION_SCHEMA_VERSION,
)
from aer.knowledge.status import KnowledgeStatus, collect_status

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_RETRIEVAL_LIMIT",
    "EXPERIENCE_INDEXED_PROPERTIES",
    "FTS_PROPERTY_WEIGHTS",
    "KNOWN_FAILURE_LABEL",
    "MAX_RETRIEVAL_LIMIT",
    "MIN_RETRIEVAL_LIMIT",
    "PREAMBLE",
    "PROJECTION_SCHEMA_VERSION",
    "UNVERIFIED_STATUS_ORDER",
    "VERIFIED_STATUSES",
    "VERIFIED_STATUS_ORDER",
    "DriftReport",
    "ExperienceContextFormatter",
    "ExperienceRetriever",
    "ExperienceSearchQuery",
    "IndexFilters",
    "IndexMatch",
    "IndexQuery",
    "IndexedExperience",
    "IndexedRunRef",
    "KnowledgeIndex",
    "KnowledgeProjector",
    "KnowledgeStatus",
    "ProjectionOutcome",
    "RebuildReport",
    "RetrievalHit",
    "RetrievalPolicy",
    "RetrievalResult",
    "collect_status",
    "fts_expression",
    "query_terms",
]
