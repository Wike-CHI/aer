"""Tests for the operator-facing shell entry points (sections 41, 42, 66, 71, 72, 75).

Shell scripts are the part of a deployment that nobody unit-tests until they fail
at 3am, so the invariants that make them safe are asserted here as text properties:
every step fatal, no command tracing, no non-interactive host-key bypass, backups
strictly before migrations, and no build tools on the production host.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.infra.conftest import SCRIPTS_DIR
from tests.infra.support import read_repo_file

SHELL_SCRIPTS = ("deploy.sh", "rollback.sh", "smoke_test.sh")


def script(name: str) -> str:
    """The text of an ops script."""
    return read_repo_file(f"scripts/{name}")


def command_lines(document: str) -> list[str]:
    """Executable lines only: comments and blank lines removed.

    Necessary because these scripts *explain* what they refuse to do ("there is no
    `alembic downgrade` here", "deliberately no `set -x`"). Matching raw text would
    forbid documenting the very behaviour the tests are protecting.
    """
    return [
        line.strip()
        for line in document.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestShellHygiene:
    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_has_a_shebang(self, name: str) -> None:
        assert script(name).startswith("#!/usr/bin/env bash")

    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_stops_at_the_first_error(self, name: str) -> None:
        """`-E -e -u -o pipefail` is what makes "any step fails -> deployment fails" true.

        Without `pipefail` a failing command on the left of a pipeline is invisible,
        and without `-E` the ERR trap disappears inside functions -- which is
        precisely where the backup and migration steps live.
        """
        assert "set -Eeuo pipefail" in script(name)

    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_never_enables_command_tracing(self, name: str) -> None:
        match = re.search(r"^\s*set\s+-[a-z]*x", script(name), re.MULTILINE)

        assert match is None, f"{name} traces commands: {match.group(0) if match else ''!r}"

    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_host_key_verification_is_never_disabled(self, name: str) -> None:
        assert "StrictHostKeyChecking=no" not in script(name)

    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_no_carriage_returns(self, name: str) -> None:
        """A CRLF script fails on Linux with `$'\\r': command not found`.

        Checked as bytes rather than text: Python's universal newlines would
        silently translate CRLF away and the test would never fail. Windows
        checkouts default to ``core.autocrlf=true``, so this is a live risk, not a
        theoretical one -- ``.gitattributes`` pins ``eol=lf`` to prevent it.
        """
        raw = (SCRIPTS_DIR / name).read_bytes()

        assert b"\r" not in raw, f"{name} has CRLF line endings"

    @pytest.mark.parametrize("name", SHELL_SCRIPTS)
    def test_every_script_is_parsable(self, name: str) -> None:
        """A syntax error in a deploy script is found during the deploy, not before it."""
        import subprocess

        completed = subprocess.run(
            ["bash", "-n", str(SCRIPTS_DIR / name)],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


class TestDeployScript:
    def test_requires_the_image_and_the_commit(self) -> None:
        document = script("deploy.sh")
        assert "no image given" in document
        assert "no git sha given" in document
        # Never defaulted: a production deployment must state what it is deploying.
        assert "IMAGE_ARG:-${IMAGE:-${AER_IMAGE:-}}" in document

    def test_refuses_a_mutable_tag(self) -> None:
        document = script("deploy.sh")
        assert "*:latest|*:)" in document, "the `latest`/tagless guard is missing"
        assert "immutable tag" in document

    def test_backs_up_before_it_migrates(self) -> None:
        document = script("deploy.sh")
        """The order is the safety property; everything else is mechanics."""
        markers = [
            "step docker pull",
            "backup_sqlite.py",
            "upgrade head",
            "smoke_test.py",
            "step record_current",
        ]
        positions = [document.index(marker) for marker in markers]

        assert positions == sorted(positions), dict(zip(markers, positions, strict=True))

    def test_a_first_deployment_has_nothing_to_back_up(self) -> None:
        document = script("deploy.sh")
        """Skipping is allowed only when the database does not exist yet."""
        assert 'if [[ -f "${HOST_DB_PATH}" ]]' in document
        assert "first deployment, nothing to back up" in document

    def test_migrations_run_inside_the_image_with_an_explicit_path(self) -> None:
        document = script("deploy.sh")
        """A wrong path would create a second, empty database outside the volume."""
        assert 'alembic -x "db_path=${CONTAINER_DB_PATH}" upgrade head' in document

    def test_the_database_path_is_resolved_after_the_env_file(self) -> None:
        document = script("deploy.sh")
        assert document.index('. "${ENV_FILE}"') < document.index(
            'CONTAINER_DB_PATH="${AER_DB_PATH'
        )

    def test_the_image_is_exported_for_compose_interpolation(self) -> None:
        document = script("deploy.sh")
        """`docker compose` reads ${AER_IMAGE} from the environment first, then .env."""
        assert 'export AER_IMAGE="${IMAGE}"' in document

    def test_the_deployment_record_is_written_atomically(self) -> None:
        document = script("deploy.sh")
        """A truncated current.env during a crash would lose the rollback target."""
        assert 'temporary="${CURRENT_ENV}.tmp.$$"' in document
        assert 'mv -- "${temporary}" "${CURRENT_ENV}"' in document

    def test_the_previous_image_is_preserved_for_rollback(self) -> None:
        document = script("deploy.sh")
        assert "AER_PREVIOUS_IMAGE=" in document

    def test_fixes_ownership_instead_of_opening_permissions(self) -> None:
        document = script("deploy.sh")
        """`chown` to the container's uid, never `chmod 777` on production data."""
        assert "chown -R 10001:10001" in document

        no_world_writable_commands = [
            line
            for line in command_lines(document)
            if re.search(r"chmod\s+[0-7]*777", line) or re.search(r"chmod\s+a\+rwx", line)
        ]
        assert no_world_writable_commands == [], no_world_writable_commands

    def test_supports_a_dry_run(self) -> None:
        document = script("deploy.sh")
        assert "--dry-run" in document
        assert "dry-run, would run" in document

    def test_the_host_never_builds_or_installs_anything(self) -> None:
        document = script("deploy.sh")
        """The server pulls one artifact; it is not a build machine."""
        lines = command_lines(document)

        for forbidden in ("docker build", "git clone", "pip install", "python -m venv"):
            offenders = [line for line in lines if forbidden in line]
            assert offenders == [], f"the production host must not run `{forbidden}`: {offenders}"

    def test_it_logs_steps_with_timestamps(self) -> None:
        document = script("deploy.sh")
        assert "[deploy %s] %s\\n" in document
        assert "date -u" in document

    def test_it_does_not_attempt_a_schema_downgrade(self) -> None:
        document = script("deploy.sh")
        offenders = [line for line in command_lines(document) if "downgrade" in line]

        assert offenders == [], f"deploy must not downgrade: {offenders}"


class TestRollbackScript:
    def test_never_downgrades_the_schema(self) -> None:
        document = script("rollback.sh")
        """A downgrade executed against data written by the newer release is the risk."""
        lines = command_lines(document)

        assert [line for line in lines if "alembic" in line] == []
        assert [line for line in lines if "upgrade" in line] == []

    def test_refuses_to_guess_a_target(self) -> None:
        document = script("rollback.sh")
        assert "no deployment record at" in document
        assert "pass --to <image>" in document

    def test_keeps_the_record_honest_when_the_old_image_cannot_run(self) -> None:
        document = script("rollback.sh")
        """A current.env that lies is worse than no current.env at all."""
        assert "The live image has NOT been changed" in document
        assert "restore_sqlite.py" in document
        assert "--force" in document

    def test_refuses_a_mutable_target(self) -> None:
        document = script("rollback.sh")
        assert "*:latest|*:)" in document

    def test_records_the_targets_own_commit(self) -> None:
        document = script("rollback.sh")
        """After a rollback the recorded commit must describe what is now live."""
        assert "sha_from_image" in document
        assert 'TARGET_SHA="$(sha_from_image "${TARGET}")"' in document

    def test_supports_a_dry_run(self) -> None:
        document = script("rollback.sh")
        assert "--dry-run" in document


class TestSmokeWrapper:
    def test_wrapper_keeps_one_implementation_of_the_checks(self) -> None:
        """The wrapper delegates; two answers to "is it up?" would be one too many."""
        document = script("smoke_test.sh")

        assert "smoke_test.py" in document
        assert 'exec "${PYTHON_BIN}"' in document
        # No checks of its own.
        assert "integrity_check" not in document
        assert "alembic" not in document

    def test_the_interpreter_can_be_overridden(self) -> None:
        assert "AER_PYTHON" in script("smoke_test.sh")

    def test_the_wrapper_exists_where_the_scripts_are(self) -> None:
        assert (SCRIPTS_DIR / "smoke_test.sh").is_file()
        assert not (Path(SCRIPTS_DIR) / "healthcheck.sh").exists(), (
            "the old name implied a long-running service to poll"
        )
