"""Fixtures for the Milestone 8 tests.

The runtime is wired to the recording index double so that an adapter-driven scenario
can include retrieval without needing the graph engine. Everything else in these tests
goes through the real protocol path: a real adapter, the real ingestor, the real
repositories, the real runtime API.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aer import AER
from aer.adapter import AdapterIngestor
from tests.knowledge.support import RecordingIndex


@pytest.fixture
def index() -> RecordingIndex:
    """A recording, non-scoring stand-in for the graph index."""
    return RecordingIndex()


@pytest.fixture
def adapter_runtime(data_dir, index: RecordingIndex) -> Iterator[AER]:
    """An open runtime whose knowledge index is the recording double."""
    runtime = AER(data_dir, knowledge_index=index)
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture
def ingestor(adapter_runtime: AER) -> AdapterIngestor:
    """The AER side of the adapter protocol, as the facade hands it out."""
    return adapter_runtime.adapter_ingestor
