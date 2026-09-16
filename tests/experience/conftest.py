"""Fixtures for the Milestone 5 tests.

``open_aer`` is a factory rather than a fixed fixture because most experience tests
need a runtime with a *specific* provider (a liar, a crasher, none at all), and a
single shared instance would silently couple them. The plain ``aer`` fixture for
runtimes without distillation comes from ``tests/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from aer import AER


@pytest.fixture
def open_aer(data_dir) -> Iterator[Callable[..., AER]]:
    """Factory for runtimes sharing one data directory.

    Every runtime it creates is closed at the end of the test, and they all point at
    the same SQLite file, which is what makes "close, reopen, read it back"
    assertions possible.
    """
    created: list[AER] = []

    def factory(provider: object | None = None, **kwargs: object) -> AER:
        runtime = AER(
            data_dir,
            distillation_provider=provider,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )
        created.append(runtime)
        return runtime

    yield factory

    for runtime in created:
        runtime.close()
