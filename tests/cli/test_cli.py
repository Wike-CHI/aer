"""The operator CLI (TASKS.md #14, Milestone 11).

Every command is driven through ``main([...])`` against a throwaway data directory:
the CLI is a lens over the runtime, so the tests assert what the lens shows, not
how it renders. Exit codes matter as much as text: unknown ids and a failed doctor
check are failures a script can branch on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aer import AER
from aer.cli import main
from tests.experience.support import (
    StaticProvider,
    build_recovery_run,
    build_unresolved_failure_run,
)

try:  # the retrieval index is an optional extra with no Windows wheel
    import neug as _neug  # noqa: F401

    HAS_NEUG = True
except ImportError:
    HAS_NEUG = False


CliRunner = Callable[..., tuple[int, str, str]]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Iterator[CliRunner]:
    """Run the CLI and return (exit code, stdout, stderr)."""

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    yield run


def run_cli(data_dir: Path, *argv: str) -> tuple[str, ...]:
    """Prefix the standard data-dir flag."""
    return ("--data-dir", str(data_dir), *argv)


def seed_recovery(data_dir: Path) -> str:
    """A verified-success run with a distilled experience."""
    with AER(data_dir, distillation_provider=StaticProvider()) as aer:
        run_id = build_recovery_run(aer)
        experience = aer.distill_run(run_id)
        assert experience is not None
        return run_id


def resolve_experience_id(data_dir: Path, short_id: str) -> str:
    """Map a listed short id back to the full experience id."""
    with AER(data_dir) as aer:
        for experience in aer.list_experiences():
            if experience.id.startswith(short_id):
                return experience.id
    raise AssertionError(f"no experience starts with {short_id}")


class TestInit:
    def test_init_creates_the_database(
        self, cli: CliRunner, data_dir: Path, tmp_path: Path
    ) -> None:
        code, out, _ = cli(*run_cli(data_dir, "init"))

        assert code == 0
        assert (tmp_path / "data" / "aer.db").exists()
        assert "initialized" in out
        assert "schema" in out

    def test_init_is_idempotent(self, cli: CliRunner, data_dir: Path) -> None:
        assert cli(*run_cli(data_dir, "init"))[0] == 0
        assert cli(*run_cli(data_dir, "init"))[0] == 0


class TestRuns:
    def test_empty_store_says_so(self, cli: CliRunner, data_dir: Path) -> None:
        code, out, _ = cli(*run_cli(data_dir, "runs"))

        assert code == 0
        assert "no runs" in out

    def test_a_run_is_listed(self, cli: CliRunner, data_dir: Path) -> None:
        run_id = seed_recovery(data_dir)

        code, out, _ = cli(*run_cli(data_dir, "runs"))

        assert code == 0
        assert run_id[:8] in out
        assert "SUCCESS" in out

    def test_status_filter(self, cli: CliRunner, data_dir: Path) -> None:
        seed_recovery(data_dir)

        code, out, _ = cli(*run_cli(data_dir, "runs", "--status", "FAILED"))

        assert code == 0
        assert "no runs" in out

    def test_run_show_prints_the_verdicts(self, cli: CliRunner, data_dir: Path) -> None:
        run_id = seed_recovery(data_dir)

        code, out, _ = cli(*run_cli(data_dir, "run", "show", run_id))

        assert code == 0
        assert run_id in out
        assert "2/2 required passed" in out
        assert "verified   : " in out

    def test_run_show_unknown_id_fails(self, cli: CliRunner, data_dir: Path) -> None:
        code, _, err = cli(*run_cli(data_dir, "run", "show", "no-such-run"))

        assert code == 1
        assert "no run with id" in err


class TestExperiences:
    def test_empty_store_says_so(self, cli: CliRunner, data_dir: Path) -> None:
        code, out, _ = cli(*run_cli(data_dir, "experience", "list"))

        assert code == 0
        assert "no experiences" in out

    def test_a_distilled_experience_is_listed(self, cli: CliRunner, data_dir: Path) -> None:
        seed_recovery(data_dir)

        code, out, _ = cli(*run_cli(data_dir, "experience", "list"))

        assert code == 0
        assert "RECOVERY" in out

    def test_experience_show_prints_the_claim_and_sources(
        self, cli: CliRunner, data_dir: Path
    ) -> None:
        run_id = seed_recovery(data_dir)

        _, out, _ = cli(*run_cli(data_dir, "experience", "list"))
        short_id = next(line.split()[0] for line in out.splitlines() if "RECOVERY" in line)
        full_id = resolve_experience_id(data_dir, short_id)

        code, out, _ = cli(*run_cli(data_dir, "experience", "show", full_id))

        assert code == 0
        assert "root cause" in out
        assert "solution" in out
        assert run_id in out  # provenance: the source run is named

    def test_experience_show_unknown_id_fails(self, cli: CliRunner, data_dir: Path) -> None:
        code, _, err = cli(*run_cli(data_dir, "experience", "show", "no-such-experience"))

        assert code == 1
        assert "no experience with id" in err


class TestSearch:
    @pytest.mark.skipif(HAS_NEUG, reason="with the index installed the failure path is gone")
    def test_search_fails_loudly_when_the_index_is_unavailable(
        self, cli: CliRunner, data_dir: Path
    ) -> None:
        """No NeuG on this platform: retrieval must say so, never silently return
        nothing (the same rule the runtime states for agents)."""
        code, _, err = cli(*run_cli(data_dir, "experience", "search", "anything"))

        assert code == 1
        assert "search failed" in err

    @pytest.mark.skipif(not HAS_NEUG, reason="needs the optional neug index")
    def test_search_finds_relevant_experience(self, cli: CliRunner, data_dir: Path) -> None:
        seed_recovery(data_dir)
        # Retrieval is a projection read: a store whose knowledge index was never
        # built has no schema to query (on CI, "Table Experience does not exist").
        # Projecting is what a real deployment does before serving search.
        with AER(data_dir) as aer:
            aer.project_experiences()

        code, out, _ = cli(*run_cli(data_dir, "experience", "search", "REST API 403"))

        assert code == 0
        assert "403" in out or "forbidden" in out.lower()


class TestStats:
    def test_counts_by_status_and_kind(self, cli: CliRunner, data_dir: Path) -> None:
        seed_recovery(data_dir)
        with AER(data_dir, distillation_provider=StaticProvider()) as aer:
            failed_run = build_unresolved_failure_run(aer)
            aer.distill_run(failed_run)

        code, out, _ = cli(*run_cli(data_dir, "stats"))

        assert code == 0
        assert "runs" in out
        assert "experiences" in out
        assert "SUCCESS" in out
        assert "FAILED" in out  # run status
        assert "FAILURE" in out  # experience kind
        assert "RECOVERY" in out


class TestDoctor:
    def test_all_checks_pass_after_init(
        self, cli: CliRunner, data_dir: Path, tmp_path: Path
    ) -> None:
        assert cli(*run_cli(data_dir, "init"))[0] == 0

        code, out, _ = cli(*run_cli(data_dir, "doctor"))

        assert code == 0
        for name in (
            "Directory Permission",
            "SQLite",
            "Migration",
            "Database Integrity",
            "FTS5",
            "Configuration",
        ):
            assert name in out
        assert "[FAIL]" not in out

    def test_missing_database_fails_with_a_hint(self, cli: CliRunner, tmp_path: Path) -> None:
        empty = tmp_path / "nowhere"

        code, out, _ = cli(*run_cli(empty, "doctor"))

        assert code == 1
        assert "Directory Permission" in out
        assert "[FAIL]" in out
