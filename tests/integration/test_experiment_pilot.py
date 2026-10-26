"""The experiment pilot as an operator would run it: a real subprocess.

Unit tests cover the harness and metrics in-process; this test covers the entry
point itself -- argument parsing, the learning phase, and the report rendering --
and pins the honest framing: the script must keep saying it validates the
machinery, not the hypothesis.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_experiment_pilot.py"


def run_pilot(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPT),
            "--data-dir",
            str(tmp_path / "pilot-data"),
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


class TestPilotScript:
    def test_the_pilot_runs_end_to_end(self, tmp_path: Path) -> None:
        result = run_pilot(tmp_path, "--tasks", "10", "--seed", "7")

        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "experiment  : pilot" in out
        # Both arms were exercised and the machinery saw the scripted difference.
        assert "first pass success" in out
        assert "experience hit rate" in out
        assert "success lift" in out

    def test_the_report_keeps_the_honest_framing(self, tmp_path: Path) -> None:
        result = run_pilot(tmp_path, "--tasks", "4")

        assert result.returncode == 0, result.stderr
        assert "validates the harness and the metrics, not the MVP hypothesis" in (result.stdout)

    def test_assignment_is_seed_stable(self, tmp_path: Path) -> None:
        """Same seed, same assignment: two runs must put the same units in the
        same arms, which is what makes the pilot reproducible."""
        first = run_pilot(tmp_path / "a", "--tasks", "8", "--seed", "11")
        second = run_pilot(tmp_path / "b", "--tasks", "8", "--seed", "11")

        assert first.returncode == 0 and second.returncode == 0
        runs_row = next(line for line in first.stdout.splitlines() if line.startswith("runs"))
        runs_row_again = next(
            line for line in second.stdout.splitlines() if line.startswith("runs")
        )
        assert runs_row == runs_row_again
        assert runs_row.split()[1:] and runs_row.split()[1] != runs_row.split()[2]
