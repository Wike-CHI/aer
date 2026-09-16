"""Deployment asset tests (Infrastructure milestone, sections 61, 65, 81).

These are tests about *files that are never imported*: the Dockerfile, the compose
file, the environment template, the ignore files and the workflows. They are worth
testing precisely because nothing else does: a typo in `.dockerignore` or a
`latest` tag in a workflow is invisible until it is expensive.

The assertions are deliberately structural rather than snapshot-based: they state
the property that matters ("no data is copied into the image", "the host key is
verified") so a legitimate edit does not fail the suite for cosmetic reasons.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.infra.support import REPOSITORY_ROOT, read_repo_file, repo_file

#: The four directories a container must be able to write, and the four bind mounts.
CONTAINER_DIRECTORIES = ("/data", "/artifacts", "/knowledge", "/backups")

#: Action versions mandated for this milestone. The older majors must not reappear:
#: v6 and later moved to the Node 24 runner, and mixing majors silently downgrades.
REQUIRED_ACTIONS = (
    "actions/checkout@v7",
    "actions/setup-python@v7",
    "docker/login-action@v4",
    "docker/build-push-action@v7",
)
FORBIDDEN_ACTIONS = (
    "actions/checkout@v4",
    "actions/setup-python@v5",
    "docker/build-push-action@v6",
)


def load_yaml(relative: str) -> dict[str, Any]:
    """Parse a YAML file from the repository."""
    return yaml.safe_load(read_repo_file(relative))


def workflow_triggers(workflow: dict[str, Any]) -> set[str]:
    """The event names a workflow reacts to.

    YAML 1.1 parsers (including PyYAML) turn the bare key ``on`` into the boolean
    ``True``, so the mapping has to be looked up both ways rather than "fixed" by
    quoting it differently in the file.
    """
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, dict):
        return set(raw)
    return set(raw or ())


def dockerfile_instructions() -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` for each Dockerfile instruction, continuations joined."""
    joined = read_repo_file("Dockerfile").replace("\\\n", " ")
    instructions: list[tuple[str, str]] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        instruction, _, rest = line.partition(" ")
        instructions.append((instruction.upper(), rest.strip()))
    return instructions


def instructions_named(name: str) -> list[str]:
    """Arguments of every instruction called ``name``."""
    return [rest for instruction, rest in dockerfile_instructions() if instruction == name]


def baked_environment() -> dict[str, str]:
    """The ``ENV`` assignments the image carries, as a mapping."""
    entries: dict[str, str] = {}
    for assignment in " ".join(instructions_named("ENV")).split():
        key, _, value = assignment.partition("=")
        if key:
            entries[key] = value
    return entries


# --- MODULE-LEVEL FIXTURES ---------------------------------------------------
# Parsing these files is pure and cheap, so each is parsed once per module.


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """The parsed compose file."""
    return load_yaml("deploy/compose.yaml")


@pytest.fixture(scope="module")
def template() -> dict[str, str]:
    """`deploy/env.example` as a mapping."""
    values: dict[str, str] = {}
    for line in read_repo_file("deploy/env.example").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    """The parsed CI workflow."""
    return load_yaml(".github/workflows/ci.yml")


@pytest.fixture(scope="module")
def deploy() -> dict[str, Any]:
    """The parsed deployment workflow."""
    return load_yaml(".github/workflows/deploy.yml")


class TestComposeFile:
    """One service, four bind mounts, no daemon, no port."""

    def test_declares_exactly_one_service(self, compose: dict[str, Any]) -> None:
        assert list(compose["services"]) == ["aer-runtime"]

    def test_the_service_is_a_one_shot_command_runner(self, compose: dict[str, Any]) -> None:
        """No `sleep infinity`, no restart policy, no port: AER is embedded.

        A container that stays up would be a service that does not exist, and the
        next person would try to monitor it.
        """
        service = compose["services"]["aer-runtime"]

        assert service.get("restart") == "no"
        assert "ports" not in service
        assert "healthcheck" not in service
        assert "command" not in service
        assert "entrypoint" not in service

    def test_the_image_reference_is_mandatory_and_not_defaulted(
        self, compose: dict[str, Any]
    ) -> None:
        image = compose["services"]["aer-runtime"]["image"]

        assert "${AER_IMAGE:" in image
        assert ":?" in image, "an unset AER_IMAGE must abort, not fall back"
        assert "latest" not in image

    def test_every_persistent_directory_is_a_bind_mount(self, compose: dict[str, Any]) -> None:
        volumes = compose["services"]["aer-runtime"]["volumes"]

        assert [volume["target"] for volume in volumes] == list(CONTAINER_DIRECTORIES)
        for volume in volumes:
            assert volume["type"] == "bind"
            # Named/anonymous volumes would hide the data from the operator.
            assert "${AER_HOST_" in volume["source"]
            assert ":?" in volume["source"]

    def test_the_container_runs_without_privileges(self, compose: dict[str, Any]) -> None:
        service = compose["services"]["aer-runtime"]

        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
        assert "privileged" not in service
        # Compose must not override the image's non-root user back to root.
        assert "user" not in service or service["user"] != "root"

    def test_there_is_a_writable_temporary_directory(self, compose: dict[str, Any]) -> None:
        """SQLite needs scratch space; without it a large sort fails mid-migration."""
        assert compose["services"]["aer-runtime"]["tmpfs"] == ["/tmp"]


class TestEnvironmentTemplate:
    """`deploy/env.example` is the only environment file that is committed."""

    @pytest.mark.parametrize(
        "key",
        [
            "AER_ENV",
            "AER_DATA_DIR",
            "AER_ARTIFACT_DIR",
            "AER_KNOWLEDGE_DIR",
            "AER_LOG_LEVEL",
            "AER_IMAGE",
            "AER_BACKUP_RETENTION",
            "AER_HOST_DATA_DIR",
            "AER_HOST_ARTIFACT_DIR",
            "AER_HOST_KNOWLEDGE_DIR",
            "AER_HOST_BACKUP_DIR",
        ],
    )
    def test_required_keys_are_present(self, template: dict[str, str], key: str) -> None:
        assert key in template, f"{key} missing from deploy/env.example"

    def test_the_container_paths_match_the_mount_targets(self, template: dict[str, str]) -> None:
        assert template["AER_DATA_DIR"] == "/data"
        assert template["AER_ARTIFACT_DIR"] == "/artifacts"
        assert template["AER_KNOWLEDGE_DIR"] == "/knowledge"
        assert template["AER_BACKUP_DIR"] == "/backups"

    def test_host_and_container_paths_use_different_names(self, template: dict[str, str]) -> None:
        """The one mistake this naming exists to prevent: mounting /data from /data."""
        assert template["AER_HOST_DATA_DIR"].startswith("/srv/aer/")
        assert template["AER_DATA_DIR"] == "/data"

    def test_no_credentials_are_committed(self, template: dict[str, str]) -> None:
        suspicious = ("TOKEN", "PASSWORD", "SECRET", "PASSWD", "API_KEY", "PRIVATE")
        offenders = [key for key in template if any(word in key.upper() for word in suspicious)]

        assert offenders == [], f"credentials must never live in a committed file: {offenders}"
        assert template["AER_IMAGE"] == "", "the live image belongs in current.env, not here"


class TestDockerfile:
    def test_uses_a_python_312_slim_base(self) -> None:
        bases = instructions_named("FROM")

        assert bases and all(base.startswith("python:3.12") for base in bases)
        assert any("slim" in base for base in bases)

    def test_creates_and_uses_a_non_root_user(self) -> None:
        users = instructions_named("USER")

        assert users, "the image must drop privileges"
        assert users[-1] == "aer"
        assert all(user != "root" for user in users)

        setup = " ".join(instructions_named("RUN"))
        assert "useradd" in setup and "--uid 10001" in setup

    def test_stamps_the_commit_into_oci_labels(self) -> None:
        """`docker inspect` has to answer "which commit is this?" without the repo."""
        labels = " ".join(instructions_named("LABEL"))

        for label in (
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
            "org.opencontainers.image.version",
        ):
            assert label in labels, f"missing {label}"
        assert "${GIT_SHA}" in labels
        # Declared as build arguments, so CI supplies the commit it verified.
        assert "GIT_SHA" in " ".join(instructions_named("ARG"))

    def test_ships_the_package_and_the_migration_scripts(self) -> None:
        copies = " ".join(instructions_named("COPY"))

        for required in ("aer/", "migrations/", "alembic.ini", "pyproject.toml", "scripts/"):
            assert required in copies, f"the runtime image must contain {required}"

    def test_installs_the_package_editable_and_without_dev_tools(self) -> None:
        """Editable is load-bearing: migrations are located by walking up from the package."""
        installs = " ".join(instructions_named("RUN"))

        assert "--editable ." in installs
        assert "[dev]" not in installs
        assert "ruff" not in installs and "mypy" not in installs and "pytest" not in installs

    def test_never_copies_the_whole_context(self) -> None:
        """`COPY . .` would defeat .dockerignore the day somebody edits it."""
        for copied in instructions_named("COPY"):
            assert copied.split() != [".", "."], "COPY . . would pull in data/ and .git"

    def test_does_not_bake_in_data_or_a_database(self) -> None:
        """The path may be configured; the file itself must never be copied in.

        Checked against the instructions rather than the raw text: the Dockerfile
        explains in a comment which path Alembic *would* have used, and a comment
        must not be able to fail this test.
        """
        sources = " ".join(instructions_named("COPY") + instructions_named("ADD"))

        assert "data" not in sources
        assert "backups" not in sources
        assert ".db" not in sources

        document = read_repo_file("Dockerfile")
        assert "sleep infinity" not in document
        assert "create_all" not in document

    def test_the_baked_environment_satisfies_the_configuration_rules(self) -> None:
        """A bare `docker run` must not fail AER's own configuration validation.

        This is the test that catches a *silently* broken image. `AER_ENV=production`
        rejects relative paths, so a directory variable the Dockerfile forgets to set
        falls back to its relative default and the image only fails in production --
        where nobody is running the test suite.
        """
        from aer.config import load_deployment_config

        environment = {k: v for k, v in baked_environment().items() if k.startswith("AER_")}
        config = load_deployment_config(environment)

        assert config.is_production is True
        assert config.db_path.as_posix() == "/data/aer.db"
        assert config.backup_dir.as_posix() == "/backups"

    def test_the_image_does_not_pin_a_database_file_that_overrides_the_directory(self) -> None:
        """Setting both AER_DB_PATH and AER_DATA_DIR makes one of them a lie.

        AER_DB_PATH wins in ``aer.config``, so a baked AER_DB_PATH would make every
        later ``-e AER_DATA_DIR=...`` silently ineffective -- the class of mistake
        the configuration module exists to prevent.
        """
        environment = baked_environment()

        assert "AER_DATA_DIR" in environment
        assert "AER_DB_PATH" not in environment, (
            "baking AER_DB_PATH defeats any runtime AER_DATA_DIR override"
        )

    def test_every_path_the_configuration_reads_is_absolute(self) -> None:
        """Every AER_* path must land inside a bind mount, not inside the image."""
        environment = baked_environment()

        for name in (
            "AER_DATA_DIR",
            "AER_ARTIFACT_DIR",
            "AER_KNOWLEDGE_DIR",
            "AER_BACKUP_DIR",
        ):
            assert name in environment, f"{name} is not baked into the image"
            assert environment[name].startswith("/"), f"{name} must be an absolute path"

    def test_default_command_is_not_a_daemon(self) -> None:
        commands = instructions_named("CMD")

        assert commands, "the image needs a default command"
        assert "sleep" not in " ".join(commands)
        # Prints the version and exits: `docker run <image>` is the smallest smoke test.
        assert "aer" in " ".join(commands)


class TestIgnoreFiles:
    def test_dockerignore_keeps_the_data_out(self) -> None:
        patterns = {
            line.strip()
            for line in read_repo_file(".dockerignore").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        for pattern in ("/.git", "/data", "/backups", "*.db", ".env", "/deploy", "/tests", "*.md"):
            assert pattern in patterns, f"{pattern} must be excluded from the build context"
        # ...without breaking the build: README.md is required by pyproject.toml.
        assert "!README.md" in patterns

    def test_dockerignore_does_not_exclude_what_the_runtime_needs(self) -> None:
        document = read_repo_file(".dockerignore")

        for required in ("migrations", "alembic.ini", "scripts"):
            # Exclusions only; a `!`-negation would be fine but a direct rule is not.
            assert f"/{required}" not in document, f"{required} must reach the image"
            assert f"\n{required}\n" not in document

    def test_gitattributes_pins_lf_line_endings(self) -> None:
        """Everything here is deployed to Linux; CRLF would break the shell scripts.

        ``core.autocrlf=true`` (the Windows default) rewrites the working copy on
        checkout, so leaving this to per-machine configuration is not sufficient.
        """
        document = read_repo_file(".gitattributes")

        assert "* text=auto eol=lf" in document
        for binary in ("*.db binary", "*.png binary"):
            assert binary in document

    def test_gitignore_covers_secrets_and_local_data(self) -> None:
        document = read_repo_file(".gitignore")

        for pattern in (".env", "backups/", "*.db", "*.db-wal", "data/*", "deploy/*.env"):
            assert pattern in document, f"{pattern} must be ignored"
        # The template stays tracked, or the next operator has nothing to copy.
        assert "!deploy/env.example" in document
        assert "!data/.gitkeep" in document


class TestWorkflows:
    def test_ci_runs_on_pull_requests_and_on_demand(self, ci: dict[str, Any]) -> None:
        assert workflow_triggers(ci) == {"pull_request", "workflow_dispatch"}

    def test_ci_has_read_only_permissions(self, ci: dict[str, Any]) -> None:
        assert ci["permissions"] == {"contents": "read"}
        assert "write-all" not in read_repo_file(".github/workflows/ci.yml")

    def test_ci_builds_and_smokes_the_container(self, ci: dict[str, Any]) -> None:
        steps = ci["jobs"]["container"]["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)

        assert "docker build" in commands
        assert "smoke_test.py --temp-db-check" in commands
        assert "docker compose --file deploy/compose.yaml config" in commands

    def test_ci_covers_the_quality_gate(self, ci: dict[str, Any]) -> None:
        commands = "\n".join(step.get("run", "") for step in ci["jobs"]["quality-gate"]["steps"])

        for command in ("ruff check .", "ruff format --check .", "mypy aer/", "pytest -q"):
            assert command in commands, f"CI must run `{command}`"

    def test_ci_never_touches_production(self) -> None:
        """No server, no production database, no secret: CI runs fully isolated."""
        document = read_repo_file(".github/workflows/ci.yml")

        assert "secrets." not in document
        assert "PRODUCTION_" not in document
        # No SSH at all: a CI job that can reach the server is a CI job that can
        # damage production while testing a pull request.
        for command in ("ssh", "scp"):
            assert re.search(rf"^\s*{command}\b", document, re.MULTILINE) is None

    def test_deploy_runs_on_main_and_on_demand(self, deploy: dict[str, Any]) -> None:
        triggers = workflow_triggers(deploy)

        assert triggers == {"push", "workflow_dispatch"}
        assert deploy[True]["push"]["branches"] == ["main"]

    def test_deploy_cannot_run_twice_against_the_same_database(
        self, deploy: dict[str, Any]
    ) -> None:
        assert deploy["concurrency"] == {"group": "aer-production", "cancel-in-progress": False}

    def test_deploy_has_the_permissions_publishing_needs_and_no_more(
        self, deploy: dict[str, Any]
    ) -> None:
        assert deploy["permissions"] == {"contents": "read", "packages": "write"}

    def test_the_deploy_job_is_bound_to_the_production_environment(
        self, deploy: dict[str, Any]
    ) -> None:
        """Declared so repository settings can require approval without editing this file."""
        assert deploy["jobs"]["deploy"]["environment"] == "production"

    def test_the_quality_gate_is_rerun_on_main(self, deploy: dict[str, Any]) -> None:
        """The merge commit is not the commit the pull request CI tested."""
        commands = "\n".join(
            step.get("run", "") for step in deploy["jobs"]["quality-gate"]["steps"]
        )

        assert "pytest -q" in commands
        assert "ruff check ." in commands
        assert deploy["jobs"]["publish"]["needs"] == ["prepare", "quality-gate"]

    def test_the_host_key_is_verified(self) -> None:
        """A forged server must be indistinguishable from the real one only if we let it be."""
        for workflow in (".github/workflows/ci.yml", ".github/workflows/deploy.yml"):
            assert "StrictHostKeyChecking=no" not in read_repo_file(workflow)

        document = read_repo_file(".github/workflows/deploy.yml")
        assert "StrictHostKeyChecking=yes" in document
        assert "PRODUCTION_KNOWN_HOSTS" in document

    def test_only_one_tag_is_published_and_it_is_the_commit(self, deploy: dict[str, Any]) -> None:
        steps = deploy["jobs"]["publish"]["steps"]
        build = next(
            step for step in steps if step.get("uses", "").startswith("docker/build-push-action")
        )

        # Exactly one tag, and it is the commit: a `latest` alias would be a second,
        # mutable name for the same bytes and a way to deploy something other than
        # what current.env records.
        assert build["with"]["tags"] == "${{ needs.prepare.outputs.image }}"
        assert "${{ needs.prepare.outputs.sha }}" in build["with"]["build-args"]

        # No step anywhere may publish a mutable tag.
        for job in deploy["jobs"].values():
            for step in job.get("steps", []):
                tags = str((step.get("with") or {}).get("tags", ""))
                assert "latest" not in tags, f"mutable tag published by {step.get('name')}"

    def test_deploy_uses_only_the_secrets_it_needs(self) -> None:
        """A stray secret is a leak waiting to happen; a registry PAT is not needed at all."""
        document = read_repo_file(".github/workflows/deploy.yml")

        assert set(re.findall(r"secrets\.([A-Z_]+)", document)) == {
            "GITHUB_TOKEN",
            "PRODUCTION_HOST",
            "PRODUCTION_USER",
            "PRODUCTION_PORT",
            "PRODUCTION_SSH_KEY",
            "PRODUCTION_KNOWN_HOSTS",
        }

    def test_workflows_use_the_current_action_majors(self) -> None:
        document = read_repo_file(".github/workflows/ci.yml") + read_repo_file(
            ".github/workflows/deploy.yml"
        )

        for action in REQUIRED_ACTIONS:
            assert action in document, f"{action} is missing"
        for action in FORBIDDEN_ACTIONS:
            assert action not in document, f"{action} is an outdated major"

    def test_no_workflow_echoes_commands(self) -> None:
        """`set -x` would print expanded values into a widely readable CI log.

        Matched as a *command*, not as a substring: these files explain in comments
        why the flag is avoided, and a substring check would forbid mentioning it.
        """
        pattern = re.compile(r"^\s*set\s+-[a-z]*x", re.MULTILINE)

        for workflow in (".github/workflows/ci.yml", ".github/workflows/deploy.yml"):
            match = pattern.search(read_repo_file(workflow))
            assert match is None, f"{workflow} enables command tracing: {match.group(0)!r}"

    def test_deploy_does_not_pass_a_registry_token_to_the_server(self) -> None:
        """The host authenticates itself once, with a read-only scope."""
        document = read_repo_file(".github/workflows/deploy.yml")

        assert (
            "docker login" not in document or "PRODUCTION" not in document.split("docker login")[0]
        )
        assert "ghcr.io" not in document.split("Copy the deployment assets")[-1]


class TestRepositoryHygiene:
    """Section 81: the repository must not contain data or credentials."""

    SCANNED_DIRECTORIES = ("aer", "scripts", "deploy", "migrations", "tests", "docs", ".github")

    SECRET_PATTERNS = (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"\bghp_[0-9A-Za-z]{36}"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}"),
    )

    LOCAL_ARTIFACTS = ("*.db", "*.db-wal", "*.db-shm", ".env")

    #: Root-level files that are part of the deployment surface, so they are scanned
    #: for credentials too (a token pasted into the Dockerfile is still a token).
    ROOT_FILES = (
        "pyproject.toml",
        "alembic.ini",
        "Dockerfile",
        ".dockerignore",
        ".gitignore",
        "README.md",
        "agent.md",
    )

    @staticmethod
    def files() -> list[Path]:
        found: list[Path] = []
        for directory in TestRepositoryHygiene.SCANNED_DIRECTORIES:
            root = repo_file(directory)
            if root.is_dir():
                found.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                )
        found.extend(
            path
            for path in (repo_file(name) for name in TestRepositoryHygiene.ROOT_FILES)
            if path.is_file()
        )
        return found

    def test_no_database_or_environment_file_is_present(self) -> None:
        offenders = [
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in self.files()
            for pattern in self.LOCAL_ARTIFACTS
            if path.match(pattern)
        ]

        assert offenders == [], f"data/environment files must not be committed: {offenders}"

    def test_no_private_key_or_token_is_present(self) -> None:
        offenders: list[str] = []
        for path in self.files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # pragma: no cover - binary assets
                continue
            if any(pattern.search(text) for pattern in self.SECRET_PATTERNS):
                offenders.append(path.relative_to(REPOSITORY_ROOT).as_posix())

        assert offenders == [], f"possible credentials committed: {offenders}"

    def test_the_deliverable_scripts_exist(self) -> None:
        for relative in (
            "Dockerfile",
            ".dockerignore",
            "deploy/compose.yaml",
            "deploy/env.example",
            "scripts/backup_sqlite.py",
            "scripts/restore_sqlite.py",
            "scripts/deploy.sh",
            "scripts/rollback.sh",
            "scripts/smoke_test.sh",
            ".github/workflows/ci.yml",
            ".github/workflows/deploy.yml",
        ):
            assert repo_file(relative).is_file(), f"missing deliverable: {relative}"
