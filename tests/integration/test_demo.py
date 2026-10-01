"""The shipped acceptance entry point must use a real, persistent retrieval index."""

from pathlib import Path

import pytest

from aer.demo import run_demo


def test_demo_reuses_verified_recovery_and_preserves_usage(tmp_path: Path) -> None:
    pytest.importorskip("neug")
    result = run_demo(tmp_path)
    usage = result["usage"]
    assert isinstance(usage, dict)
    assert usage["retrieval_count"] == 1
    assert usage["injection_count"] == 1
    assert usage["explicit_adoption_count"] == 1
    assert usage["verified_success_runs"] == 1
    assert usage["verified_failure_runs"] == 0
    assert (tmp_path / "data" / "aer.db").is_file()
