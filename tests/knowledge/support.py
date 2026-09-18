"""Test doubles for the Milestone 6 tests.

There are two jobs a fake index could do, and conflating them is how a test suite
ends up proving things about itself:

* **stand in for the store of projections**, so the projector's semantics --
  idempotency, multi-source, deprecation, deletion -- can be checked on every
  machine, including this one, where NeuG has no wheel;
* **act as a search engine**, which it must not do.

So :class:`RecordingIndex` records what it was asked to write and returns whatever
search results the test scripted. It never decides relevance. Ranking, Chinese
tokenisation, BM25 direction and "does the graph filter actually constrain the
result set" are properties of the engine, and they are tested against the engine in
``test_neug_index.py`` / ``test_neug_retrieval.py``, which skip when it is absent
and run for real on Linux CI and on the deployment target.

The alternative -- a fake that scores keyword overlap -- would pass on Windows and
tell us nothing about production, which is precisely the failure mode
``docs/DECISIONS.md`` D-050 was written about.
"""

from __future__ import annotations

from dataclasses import replace

from aer.knowledge.base import (
    IndexedExperience,
    IndexedRunRef,
    IndexFilters,
    IndexMatch,
    IndexQuery,
)

DEFAULT_CREATED = "2026-09-01T00:00:00+00:00"
DEFAULT_UPDATED = "2026-09-01T00:00:00+00:00"


def indexed_experience(**overrides: object) -> IndexedExperience:
    """A projected experience with sensible defaults, overridable per test."""
    payload: dict[str, object] = {
        "id": "exp-1",
        "kind": "RECOVERY",
        "status": "VERIFIED",
        "domain": "wordpress",
        "title": "WordPress REST API 403",
        "problem": "WordPress REST API 403 应用密码无效",
        "root_cause": "权限不足",
        "solution": "改用有 edit_posts 权限的应用密码",
        "failed_attempts_text": "盲目重试",
        "avoid_text": "不要盲目重试",
        "outcome_verified": True,
        "generalizable": True,
        "created_at": DEFAULT_CREATED,
        "updated_at": DEFAULT_UPDATED,
        "run_ids": ("run-1",),
    }
    payload.update(overrides)
    return IndexedExperience(**payload)  # type: ignore[arg-type]


def indexed_match(**overrides: object) -> IndexMatch:
    """A search hit as the engine would return it."""
    payload: dict[str, object] = {
        "experience_id": "exp-1",
        "kind": "RECOVERY",
        "status": "VERIFIED",
        "domain": "wordpress",
        "title": "WordPress REST API 403",
        "problem": "WordPress REST API 403 应用密码无效",
        "root_cause": "权限不足",
        "solution": "改用有 edit_posts 权限的应用密码",
        "failed_attempts_text": "盲目重试",
        "avoid_text": "不要盲目重试",
        "outcome_verified": True,
        "generalizable": True,
        "created_at": DEFAULT_CREATED,
        "updated_at": DEFAULT_UPDATED,
        "bm25_score": -2.0,
    }
    payload.update(overrides)
    return IndexMatch(**payload)  # type: ignore[arg-type]


class RecordingIndex:
    """A :class:`~aer.knowledge.base.KnowledgeIndex` that records and replays.

    Implements the protocol structurally, like every other test double in this
    project -- AER's protocols are deliberately not base classes, so a double does
    not have to inherit from anything to be accepted.

    Search results are *scripted*: each call to :meth:`search` pops the next list
    from a queue, or returns the one configured as a default. That makes
    "which filters were asked for" and "what happens to these candidates"
    independently observable, which is the whole reason the retriever was split into
    a policy step and a ranking step.
    """

    def __init__(
        self,
        *,
        search_results: list[list[IndexMatch]] | None = None,
        schema_version: int | None = 1,
        fail_on_ensure: BaseException | None = None,
        fail_on_upsert: BaseException | None = None,
    ) -> None:
        self.upserts: list[tuple[tuple[IndexedExperience, ...], tuple[IndexedRunRef, ...]]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.searches: list[IndexQuery] = []
        self.resets = 0
        self.ensure_calls = 0
        self.closed = 0
        self.source_count_calls: list[tuple[str, ...]] = []

        self._experiences: dict[str, IndexedExperience] = {}
        self._run_refs: dict[str, IndexedRunRef] = {}
        self._source_counts: dict[str, int] = {}
        self._records: dict[str, int] = {}
        self._scripted = list(search_results or [])
        self._default_results: list[IndexMatch] = []
        self._schema_version = schema_version
        self._fail_on_ensure = fail_on_ensure
        self._fail_on_upsert = fail_on_upsert

    # -- protocol ----------------------------------------------------------

    def ensure_schema(self) -> None:
        self.ensure_calls += 1
        if self._fail_on_ensure is not None:
            raise self._fail_on_ensure

    def projection_version(self) -> int | None:
        return self._schema_version

    def upsert_experiences(
        self,
        experiences: tuple[IndexedExperience, ...],
        run_refs: tuple[IndexedRunRef, ...],
    ) -> None:
        if self._fail_on_upsert is not None:
            raise self._fail_on_upsert
        self.upserts.append((experiences, run_refs))
        for ref in run_refs:
            self._run_refs[ref.id] = ref
        for experience in experiences:
            # Replace, never accumulate: the engine's upsert is a replacement too,
            # and a double that appended would let a non-idempotent projector pass.
            self._experiences[experience.id] = experience
            self._source_counts[experience.id] = len(set(experience.run_ids))

    def delete_experiences(self, experience_ids: tuple[str, ...]) -> None:
        self.deleted.append(experience_ids)
        for experience_id in experience_ids:
            self._experiences.pop(experience_id, None)
            self._source_counts.pop(experience_id, None)

    def search(self, query: IndexQuery) -> list[IndexMatch]:
        self.searches.append(query)
        if self._scripted:
            return list(self._scripted.pop(0))
        return list(self._default_results)

    def source_counts(self, experience_ids: tuple[str, ...]) -> dict[str, int]:
        self.source_count_calls.append(experience_ids)
        return {
            experience_id: self._source_counts.get(experience_id, 0)
            for experience_id in experience_ids
        }

    def fingerprints(self) -> dict[str, str]:
        return {
            experience_id: experience.updated_at
            for experience_id, experience in self._experiences.items()
        }

    def count_experiences(self, *, include_deprecated: bool = True) -> int:
        return len(self._experiences)

    def reset(self) -> None:
        self.resets += 1
        self._experiences.clear()
        self._run_refs.clear()
        self._source_counts.clear()

    def close(self) -> None:
        self.closed += 1

    # -- test helpers ------------------------------------------------------

    @property
    def projected(self) -> dict[str, IndexedExperience]:
        """What the index currently holds, by id."""
        return dict(self._experiences)

    @property
    def run_refs(self) -> dict[str, IndexedRunRef]:
        """Reference nodes the index currently holds, by id."""
        return dict(self._run_refs)

    @property
    def written_count(self) -> int:
        """Total experiences written across every upsert call."""
        return sum(len(experiences) for experiences, _ in self.upserts)

    def script_results(
        self,
        results: list[list[IndexMatch]] | None = None,
        *,
        default: list[IndexMatch] | None = None,
    ) -> None:
        """Set the queue (or the fallback) of search results."""
        if results is not None:
            self._scripted = list(results)
        if default is not None:
            self._default_results = list(default)

    def corrupt(self, experience_id: str, **overrides: object) -> None:
        """Change a stored projection without going through the projector.

        Stands in for "someone edited the knowledge database" and for any drift that
        is not explained by a normal projection. Used to prove that SQLite wins after
        a rebuild.
        """
        current = self._experiences[experience_id]
        self._experiences[experience_id] = replace(current, **overrides)  # type: ignore[arg-type]

    def matching_filters(self, text: str = "", domain: str | None = None) -> list[IndexQuery]:
        """Every recorded search whose text equals ``text``."""
        return [query for query in self.searches if query.text == text and query.domain == domain]


def filters_of(index: RecordingIndex, position: int) -> IndexFilters:
    """The filters of the search call at ``position``."""
    return index.searches[position].filters
