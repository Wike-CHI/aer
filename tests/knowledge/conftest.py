"""Fixtures for the Milestone 6 tests.

Two kinds of test live here, and the fixtures are shaped so the difference is
visible at a glance:

* tests that use ``knowledge_index`` (a :class:`~tests.knowledge.support.RecordingIndex`)
  run anywhere -- they check AER's own logic: policy, ranking, wording, projection
  semantics;
* tests that use ``neug_index`` need the real engine and skip when it is absent.

The second group is deliberately not replaced by the first. A fake index cannot
tell us whether a Chinese query matches, whether BM25 points the way we think, or
whether the graph filter constrains anything -- and those are exactly the
assumptions this milestone would fail on.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aer import AER, Experience, ExperienceKind, ExperienceStatus
from aer.knowledge.projector import KnowledgeProjector
from tests.knowledge.support import RecordingIndex

#: A fixed instant, so freshness scores are arithmetic rather than clock reads.
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def knowledge_index() -> RecordingIndex:
    """A recording, non-scoring stand-in for the graph index."""
    return RecordingIndex()


@pytest.fixture
def knowledge_runtime(data_dir, knowledge_index: RecordingIndex) -> Iterator[AER]:
    """A runtime whose knowledge index is the recording double.

    Injected rather than built from ``knowledge_dir`` so that no test which happens
    to touch retrieval can accidentally create a real graph database -- or fail
    because the engine is missing.
    """
    runtime = AER(data_dir, knowledge_index=knowledge_index)
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture
def projector(knowledge_runtime: AER, knowledge_index: RecordingIndex) -> KnowledgeProjector:
    """A projector wired to the recording index."""
    return knowledge_runtime.knowledge_projector


def store_experience(
    runtime: AER,
    *,
    experience_id: str = "exp-1",
    kind: ExperienceKind = ExperienceKind.RECOVERY,
    status: ExperienceStatus = ExperienceStatus.VERIFIED,
    domain: str = "wordpress",
    title: str = "WordPress REST API 403",
    problem: str = "WordPress REST API 403 应用密码无效",
    root_cause: str | None = "应用密码权限不足",
    solution: str | None = "改用有 edit_posts 权限的应用密码",
    failed_attempts: tuple[str, ...] = ("盲目重试",),
    avoid: tuple[str, ...] = ("不要盲目重试",),
    outcome_verified: bool = True,
    source_run_id: str | None = None,
    updated_at: datetime | None = None,
) -> Experience:
    """Write an experience straight through the repository.

    Bypasses distillation on purpose: these tests are about the knowledge plane, and
    driving a whole run/verify/distil pipeline to get one row would make every
    failure ambiguous between the pipeline and the projector.
    """
    stamp = updated_at or NOW - timedelta(days=10)
    experience = Experience(
        id=experience_id,
        kind=kind,
        domain=domain,
        title=title,
        problem=problem,
        dedup_key=f"{kind.value}|{domain}|{title}|{problem}",
        root_cause=root_cause,
        solution=solution,
        failed_attempts=failed_attempts,
        avoid=avoid,
        status=status,
        outcome_verified=outcome_verified,
        created_at=NOW - timedelta(days=30),
        updated_at=stamp,
    )
    return runtime.experiences.create(experience, source_run_id=source_run_id)


def store_run(runtime: AER, run_id: str, **overrides: object) -> str:
    """Create a finished run directly, for ``RunRef`` projection tests."""
    run = runtime.start_run(task="knowledge plane test run", run_id=run_id, **overrides)  # type: ignore[arg-type]
    run.success()
    return run_id
