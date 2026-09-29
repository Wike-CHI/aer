"""Fixtures for the Codex adapter tests.

Two kinds of payload live in ``fixtures/`` and the tests keep them apart on purpose:

* **captured** -- verbatim bytes from the installed Codex CLI, produced by
  ``scripts/probe_codex_hooks.py``. The manifest records the version and the mode;
* **contract** -- written against field names taken from the CLI's own serializer, for
  the hooks nobody has been able to observe yet. These pin this repository's tolerance
  for a shape; they are not evidence about what Codex sends, and a test that treats them
  as evidence would be exactly the mistake section 45 warns about.

``MANIFEST.json`` declares which is which, and a test asserts the two agree, so a
fixture cannot quietly stop being what it claims to be.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from aer import AER
from aer.adapter.codex import CodexAdapter
from tests.knowledge.support import RecordingIndex

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, object]:
    """Load one fixture payload by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def manifest() -> dict[str, object]:
    """The fixture provenance manifest."""
    return json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


@pytest.fixture
def codex_adapter() -> CodexAdapter:
    """An adapter with a fixed identity, so tests never depend on the host's CLI."""
    return CodexAdapter(agent_version="0.155.1", detect_version=False)


@pytest.fixture
def codex_runtime(
    data_dir: Path, codex_adapter: CodexAdapter, index: RecordingIndex
) -> Iterator[AER]:
    """An open runtime wired to the *shared* recording index double.

    The same instance the test scripts, not a fresh one: a runtime with its own private
    index would return nothing for a scripted search, and the test would fail somewhere
    far away from the cause.
    """
    runtime = AER(data_dir, knowledge_index=index)
    try:
        yield runtime
    finally:
        runtime.close()
