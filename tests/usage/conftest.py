"""Fixtures for the Milestone 7 tests.

The runtime is wired to the recording index double rather than to NeuG, for the same
reason the Milestone 6 tests are: these tests are about AER's own rules -- what gets
recorded, what a report computes, when a promotion is allowed -- and none of those
questions needs a search engine that actually searches. The engine's own behaviour is
covered by the tests that skip when it is absent.

``usage_runtime`` deliberately does **not** pass a distillation provider. Nothing in
this milestone distils, and a runtime that could would make it ambiguous whether a
test's experience came from the pipeline or from the fixture.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aer import AER
from tests.knowledge.support import RecordingIndex


@pytest.fixture
def index() -> RecordingIndex:
    """A recording, non-scoring stand-in for the graph index."""
    return RecordingIndex()


@pytest.fixture
def usage_runtime(data_dir, index: RecordingIndex) -> Iterator[AER]:
    """An open runtime whose knowledge index is the recording double."""
    runtime = AER(data_dir, knowledge_index=index)
    try:
        yield runtime
    finally:
        runtime.close()
