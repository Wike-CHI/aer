"""Open-source release infrastructure (Release milestone).

Six things decide whether a stranger can install AER, use it, contribute to it and
report a problem against it: the LICENSE, the packaging metadata in
``pyproject.toml``, the ``setup.py`` + ``MANIFEST.in`` + ``package-data`` trio that
puts the migration scripts inside the wheel, and ``.github/workflows/publish.yml``.
They are all *files nothing imports*, which is how a mistake in one reaches a user
undetected.

Three assertions here are unusual enough to justify themselves:

* the LICENSE is checked by **hash**, not by looking for the word "Apache". The
  requirement is that the text is unmodified, and a substring check would happily
  pass on a licence somebody had retyped;
* the ``package-data`` patterns are checked against the **actual contents of
  ``migrations/``**. The failure this prevents -- a revision that exists in the
  repository but not in the wheel -- is invisible until a user's first ``AER(...)``
  raises ``StorageError``, which is exactly the failure D-064 was written about;
* the publish workflow is checked for **ordering**, not just content. "Builds before
  it uploads" and "validates before it uploads" are the properties; a workflow can
  contain every correct command and still run them in an order that publishes bytes
  nobody verified.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from aer import __version__
from aer.storage import migrations as migrations_module
from tests.infra.support import REPOSITORY_ROOT, read_repo_file, repo_file

#: sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt (retrieved 2026-09-18).
#: Apache-2.0 requires the licence text to accompany the work and does not permit a
#: paraphrase, so pinning the hash turns "did somebody edit it?" into a test rather
#: than a review question.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

#: The nine numbered sections of the licence, in order. Present so that a truncated
#: file fails with a message naming what is missing rather than only "hash differs".
APACHE_2_0_SECTIONS = (
    "1. Definitions.",
    "2. Grant of Copyright License.",
    "3. Grant of Patent License.",
    "4. Redistribution.",
    "5. Submission of Contributions.",
    "6. Trademarks.",
    "7. Disclaimer of Warranty.",
    "8. Limitation of Liability.",
    "9. Accepting Warranty or Additional Liability.",
    "APPENDIX: How to apply the Apache License to your work.",
)


@pytest.fixture(scope="module")
def project() -> dict[str, Any]:
    """The parsed ``[project]`` table."""
    return tomllib.loads(read_repo_file("pyproject.toml"))["project"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def publish_workflow() -> dict[str, Any]:
    """The parsed publish workflow."""
    return yaml.safe_load(read_repo_file(".github/workflows/publish.yml"))  # type: ignore[no-any-return]


def workflow_triggers(workflow: dict[str, Any]) -> set[str]:
    """The event names a workflow reacts to.

    YAML 1.1 parsers (including PyYAML) turn the bare key ``on`` into the boolean
    ``True``, so the mapping has to be looked up both ways.
    """
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, dict):
        return set(raw)
    return set(raw or ())


def steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    """The steps of one job, in order."""
    return list(workflow["jobs"][job]["steps"])


def step_actions(workflow: dict[str, Any], job: str) -> list[str]:
    """Each step's executable content (its ``uses`` or its ``run``), in order.

    Parsed from the YAML rather than searched for in the raw file, and that is not
    pedantry: the header comments of ``publish.yml`` explain why
    ``pypa/gh-action-pypi-publish`` is the right pin, so a substring search over the
    document reports that *mention* as "the upload step" and any ordering assertion
    built on it is meaningless. A parser sees only what executes.
    """
    actions: list[str] = []
    for step in steps(workflow, job):
        if "uses" in step:
            actions.append(str(step["uses"]))
        run = str(step.get("run", "")).strip()
        if run:
            actions.append(run)
    return actions


def index_of(actions: list[str], needle: str) -> int:
    """Position of the step whose action contains ``needle``.

    Raises rather than returning -1: for an ordering assertion, "the step is
    missing" and "the step runs first" must not look the same.
    """
    for index, action in enumerate(actions):
        if needle in action:
            return index
    raise AssertionError(f"no step contains {needle!r}; found:\n" + "\n---\n".join(actions))


def only_job_actions(publish_workflow: dict[str, Any]) -> list[str]:
    """The ordered actions of the single publish job."""
    jobs = list(publish_workflow["jobs"])
    assert len(jobs) == 1, f"expected one job, found {jobs}"
    return step_actions(publish_workflow, jobs[0])


def read_package_name() -> str:
    """The declared distribution name, as written in ``pyproject.toml``.

    Note that this is *not* the name that appears in artifact filenames: see
    ``test_the_artifact_name_check_uses_the_normalised_distribution_name``.
    """
    return str(tomllib.loads(read_repo_file("pyproject.toml"))["project"]["name"])


def declared_package_data() -> list[str]:
    """The ``package-data`` patterns declared for the ``aer`` package.

    Empty by design: the migration scripts are *staged* by ``setup.py`` rather than
    declared here, because a ``package-data`` pattern resolves relative to the source
    package directory, where ``_migrations`` does not exist. See D-064 and the note in
    ``pyproject.toml``. Kept as a function so the reason lives next to the absence.
    """
    data = tomllib.loads(read_repo_file("pyproject.toml"))
    return list(data["tool"]["setuptools"].get("package-data", {}).get("aer", []))


def readme_python_requirement() -> str:
    """The README's statement of the supported Python version."""
    matching = [
        line for line in read_repo_file("README.md").splitlines() if "Python >= 3.12" in line
    ]
    return "\n".join(matching)


class TestLicense:
    def test_is_the_unmodified_apache_license(self) -> None:
        content = repo_file("LICENSE").read_bytes()

        assert hashlib.sha256(content).hexdigest() == APACHE_2_0_SHA256, (
            "LICENSE must be the verbatim Apache License 2.0 text; if this fails, "
            "restore it from https://www.apache.org/licenses/LICENSE-2.0.txt rather "
            "than editing it"
        )

    def test_contains_every_section(self) -> None:
        """Redundant next to the hash on purpose: this says *what* is missing."""
        text = read_repo_file("LICENSE")

        for section in APACHE_2_0_SECTIONS:
            assert section in text, f"the licence is missing {section!r}"
        assert "Apache License" in text
        assert "Version 2.0, January 2004" in text

    def test_is_declared_in_the_modern_metadata_form(self, project: dict[str, Any]) -> None:
        """PEP 639: an SPDX expression plus `license-files`.

        The older `license = { text = "Apache-2.0" }` table still builds but warns
        that it stops working in 2027, so this also pins the migration that already
        happened.
        """
        assert project["license"] == "Apache-2.0"
        assert project["license-files"] == ["LICENSE"]

    def test_does_not_also_declare_a_license_classifier(self, project: dict[str, Any]) -> None:
        """setuptools rejects the combination, so it must not creep back in."""
        offenders = [c for c in project.get("classifiers", []) if c.startswith("License ::")]

        assert offenders == [], f"drop the license classifier(s): {offenders}"

    def test_the_readme_links_to_it(self) -> None:
        readme = read_repo_file("README.md")

        assert "](LICENSE)" in readme
        assert "Apache License 2.0" in readme


class TestVersionAndMetadata:
    def test_the_packaged_version_matches_the_runtime(self, project: dict[str, Any]) -> None:
        """Two version strings, one truth.

        They were genuinely out of step once -- ``pyproject.toml`` said 0.4.0 while
        ``aer.__version__`` said 0.5.0, and the Dockerfile's default command prints
        the latter -- so the equality is asserted rather than assumed.
        """
        assert project["version"] == __version__

    def test_the_python_floor_is_the_one_the_readme_documents(
        self, project: dict[str, Any]
    ) -> None:
        assert project["requires-python"] == ">=3.12"
        assert readme_python_requirement(), "the README must state the supported Python version"

    def test_the_declared_dependencies_are_the_ones_the_code_imports(
        self, project: dict[str, Any]
    ) -> None:
        """A missing runtime dependency is an ImportError in somebody's venv."""
        names = {re.split(r"[<>=!~]", dep)[0] for dep in project["dependencies"]}

        assert names == {"alembic", "pydantic", "sqlalchemy"}

    def test_the_engine_is_an_extra_and_not_a_requirement(self, project: dict[str, Any]) -> None:
        """D-065: a required `neug` makes `pip install aer-runtime` impossible on Windows.

        Measured, not assumed: `neug==0.2.0` publishes wheels for macOS and manylinux
        only -- no Windows wheel and no sdist -- so requiring it failed the whole
        install with `No matching distribution found for neug==0.2.0`, even though the
        runtime, the verifiers, distillation and the store all work on Windows.
        """
        extras = project["optional-dependencies"]

        assert "neug==0.2.0" in extras["knowledge"], "the engine must stay reachable"
        assert not any(dep.startswith("neug") for dep in project["dependencies"])
        # Deliberately not in `dev` either: the test suite has to be installable on
        # every platform, and CI asks for `.[dev,knowledge]` explicitly.
        assert not any(dep.startswith("neug") for dep in extras["dev"])

    def test_importing_aer_does_not_need_the_engine(self) -> None:
        """The extra is only honest while `import aer` works without the engine.

        D-065 moved `neug` out of the required dependencies. That is defensible only
        if nothing imports it eagerly, so the claim is tested by *blocking* it and
        importing the package in a subprocess. Blocking is what makes the test mean
        something in both environments: here the engine is absent anyway, and in CI it
        is installed -- where only an active blocker can prove it is not needed.
        """
        script = "\n".join(
            [
                "import sys",
                "",
                "class BlockEngine:",
                "    def find_spec(self, name, path=None, target=None):",
                "        if name == 'neug' or name.startswith('neug.'):",
                "            raise ImportError('engine blocked for this test')",
                "        return None",
                "",
                "sys.meta_path.insert(0, BlockEngine())",
                "import aer",
                "print(aer.__version__)",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == __version__

    def test_points_at_the_repository(self, project: dict[str, Any]) -> None:
        urls = project.get("urls", {})

        assert urls.get("Repository") == "https://github.com/Wike-CHI/aer"
        assert "Issues" in urls


class TestPackaging:
    def test_the_repository_copy_is_preferred_over_the_packaged_one(self) -> None:
        """D-064's ordering, asserted because getting it backwards is silent.

        A wheel carries its own copy of the migration scripts. In a checkout or an
        editable install both copies can exist -- the packaged one would be whatever
        an earlier build happened to leave behind -- so the repository must win, or a
        developer migrates a schema they are not looking at.
        """
        assert migrations_module._repository_root() == REPOSITORY_ROOT

    def test_the_bundle_destination_is_where_the_runtime_looks(self) -> None:
        """The two halves of D-064 live in different files and must agree.

        ``setup.py`` decides *where* the migration scripts are staged; the runtime
        decides *where it looks*. Nothing else checks that the two paths are the same,
        and a wheel built with them out of step fails with ``StorageError`` on a
        user's first ``AER(...)`` -- which is exactly the failure D-064 removes.
        """
        shim = read_repo_file("setup.py")
        # `aer/storage/migrations.py` -> `aer/storage` -> `aer`
        package_dir = Path(migrations_module.__file__).resolve().parent.parent
        runtime_fallback = migrations_module._PACKAGED_ROOT
        expected_fallback = package_dir / "_migrations"

        # staging target: `<build_lib>/aer/_migrations`
        assert 'PACKAGE_MIGRATIONS = Path("aer") / "_migrations"' in shim
        # runtime fallback: `<package>/_migrations`
        assert runtime_fallback == expected_fallback

        # ...mirroring the repository layout `_repository_root()` expects beneath it,
        # so both of these have to be what the build bundles and what the repository has.
        for name in ("alembic.ini", "migrations"):
            assert f'"{name}"' in shim, f"setup.py must bundle {name}"
        assert (REPOSITORY_ROOT / "alembic.ini").is_file()
        assert (REPOSITORY_ROOT / "migrations").is_dir()

    def test_the_migration_scripts_are_not_also_declared_as_package_data(self) -> None:
        """A `package-data` pattern here would be dead config that reads as insurance.

        It resolves relative to the source package directory, where `_migrations`
        never exists, so it would match nothing while looking like the mechanism that
        puts the scripts in the wheel -- and the next person to touch packaging would
        reason from it.
        """
        assert declared_package_data() == []

    def test_the_manifest_carries_everything_the_wheel_is_built_from(self) -> None:
        """`python -m build` builds the wheel *from the sdist*.

        So anything the sdist drops is also missing from the wheel, and the copy
        step in ``setup.py`` would have nothing to copy.
        """
        directives = {
            line.strip()
            for line in read_repo_file("MANIFEST.in").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        for required in ("include LICENSE", "include alembic.ini", "include setup.py"):
            assert required in directives, f"MANIFEST.in must declare {required!r}"
        # The manifest has to ship itself: the wheel built from this sdist runs from
        # a tree whose only copy of setup.py/MANIFEST.in is the one in the sdist.
        assert "include MANIFEST.in" in directives

        recursive = [d for d in directives if d.startswith("recursive-include migrations")]
        assert recursive, "MANIFEST.in must include the migration scripts"
        assert "*.py" in recursive[0] and "*.mako" in recursive[0]

    def test_the_build_shim_is_wired_into_the_distribution(self) -> None:
        """Declaring package-data is not enough: the files must be staged first."""
        document = read_repo_file("setup.py")

        assert 'cmdclass={"build_py": build_py}' in document
        assert 'PACKAGE_MIGRATIONS = Path("aer") / "_migrations"' in document
        # A stale staged copy is worse than none: `python -m build` reuses build/.
        assert "shutil.rmtree" in document


class TestPublishWorkflow:
    def test_triggers_on_a_published_release(self, publish_workflow: dict[str, Any]) -> None:
        raw = publish_workflow.get("on", publish_workflow.get(True))

        assert {"release", "workflow_dispatch"} <= workflow_triggers(publish_workflow)
        # `published`, not `created`: a draft release must not reach PyPI.
        assert raw["release"] == {"types": ["published"]}

    def test_the_workflow_level_permissions_are_empty(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        """An empty default means a new job cannot inherit a capability by accident."""
        assert publish_workflow["permissions"] == {}

    def test_the_upload_job_has_exactly_the_scopes_trusted_publishing_needs(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        for job in publish_workflow["jobs"].values():
            assert job["permissions"] == {"id-token": "write", "contents": "read"}

    def test_declares_the_pypi_environment(self, publish_workflow: dict[str, Any]) -> None:
        """The environment name is part of the PyPI Trusted Publisher registration.

        Changing it here without changing it on PyPI breaks publishing, and the
        failure appears as a rejected OIDC exchange rather than a missing setting.
        """
        for job in publish_workflow["jobs"].values():
            assert job["environment"]["name"] == "pypi"
            assert job["environment"]["url"] == "https://pypi.org/p/aer-runtime"

    def test_uses_trusted_publishing_and_no_long_lived_credential(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        document = read_repo_file(".github/workflows/publish.yml")

        assert "pypa/gh-action-pypi-publish@release/v1" in document
        # A token in a repository is a token that can leak. It must not exist here.
        assert re.search(r"secrets\.[A-Za-z_]+", document) is None, "no secret may be referenced"
        for forbidden in ("TWINE_PASSWORD", "TWINE_USERNAME", "PYPI_API_TOKEN", "PYPI_TOKEN"):
            assert forbidden not in document, f"{forbidden} must never appear in this workflow"
        assert "password:" not in document

    def test_the_artifact_name_check_uses_the_normalised_distribution_name(self) -> None:
        """`python -m build` writes `aer_runtime-0.6.0.tar.gz`, not `aer-runtime-...`.

        The distribution name is normalised (hyphens become underscores; PEP 427 /
        PEP 503), and the first dry run of `publish.yml` failed on precisely that: the
        check looked for `aer-runtime-0.6.0.tar.gz`, which has never existed. Asserted
        here because the failure mode is a red workflow at release time, which is the
        worst possible moment to discover a spelling rule.
        """
        document = read_repo_file(".github/workflows/publish.yml")
        normalised = re.sub(r"[-_.]+", "_", read_package_name())

        assert normalised == "aer_runtime", f"unexpected normalisation: {normalised}"
        assert 're.sub(r"[-_.]+", "_", name)' in document, "the name must be derived, not repeated"
        assert "dist/${dist_name}-${version}.tar.gz" in document
        # A hardcoded literal is what broke the first dry run.
        assert "dist/aer-runtime" not in document

    def test_the_upload_job_also_builds_the_artifact(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        """One job, deliberately: the bytes uploaded are the bytes verified."""
        actions = only_job_actions(publish_workflow)

        assert any("python -m build" in action for action in actions)

    def test_builds_and_validates_before_it_uploads(self, publish_workflow: dict[str, Any]) -> None:
        actions = only_job_actions(publish_workflow)

        built = index_of(actions, "python -m build")
        checked = index_of(actions, "python -m twine check")
        uploaded = index_of(actions, "pypa/gh-action-pypi-publish")

        assert built < checked < uploaded, "metadata must be validated before anything is uploaded"
        # `--strict` is what turns an unrenderable README into a failure instead of a
        # silently mangled project page.
        assert "python -m twine check --strict dist/*" in read_repo_file(
            ".github/workflows/publish.yml"
        )

    def test_verifies_the_tag_agrees_with_the_packaged_version(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        """A tag that disagrees with `project.version` publishes an uncorrelatable release."""
        actions = only_job_actions(publish_workflow)

        guard = index_of(actions, "does not match project.version")
        # and the guard runs before the build, so a mismatch costs no build time
        assert guard < index_of(actions, "python -m build")
        assert 'data["project"]["version"]' in "\n".join(actions)

    def test_exercises_the_built_wheel_before_uploading(
        self, publish_workflow: dict[str, Any]
    ) -> None:
        """`twine check` validates metadata; it cannot tell you the package runs.

        The wheel carries its own copy of the migration scripts, so a packaging
        regression shows up as `StorageError` on the user's first `AER(...)` -- not
        as a metadata error. The release gate installs the built wheel in a clean
        virtual environment and constructs a real store with it.
        """
        actions = only_job_actions(publish_workflow)

        installs_wheel = index_of(actions, "pip install dist/*.whl")
        creates_store = index_of(actions, "from aer import AER")
        uploaded = index_of(actions, "pypa/gh-action-pypi-publish")

        # Installing the wheel and using it are deliberately the *same* step: the
        # throwaway virtual environment is created, used and removed in one shell, so
        # no half-built state can leak into a later step. What has to hold is that
        # both happen before the upload.
        assert installs_wheel < uploaded
        assert creates_store < uploaded
        # A clean venv is the point: the workspace has an editable install and the
        # whole checkout, either of which would mask a wheel that cannot stand alone.
        assert any("python -m venv" in action for action in actions)
        assert any("rm -rf" in action for action in actions), "the throwaway venv must be removed"

    def test_never_traces_commands(self) -> None:
        """`set -x` would print expanded values into a widely readable CI log.

        Matched as a *command*, not as a substring: the surrounding comments explain
        why the flag is avoided, and a substring check would forbid mentioning it.
        """
        pattern = re.compile(r"^\s*set\s+-[a-z]*x", re.MULTILINE)

        match = pattern.search(read_repo_file(".github/workflows/publish.yml"))
        assert match is None, f"publish.yml enables command tracing: {match.group(0)!r}"


class TestCommunityFiles:
    """The files GitHub surfaces to a visitor deciding whether to trust the project."""

    def test_security_document_states_the_supported_versions(self) -> None:
        document = read_repo_file("SECURITY.md")

        assert "## 支持的版本" in document
        assert "0.6.x" in document
        # A `pip install aer-runtime` user needs to know which line the policy
        # refuses, and the honest answer during 0.x is "the previous ones".
        assert "< 0.6" in document

    def test_security_document_does_not_ask_for_a_public_disclosure(self) -> None:
        document = read_repo_file("SECURITY.md")

        assert "Private Vulnerability Reporting" in document
        assert "请不要用公开 issue 报告漏洞" in document
        assert "降级流程" in document, "PVR is disabled on the repository; a fallback must exist"
        # Inventing a security mailbox would be worse than saying there is none.
        assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", document) is None, (
            "no e-mail address may be invented; the fallback flow goes through an issue"
        )

    def test_security_document_does_not_overstate_the_sanitiser(self) -> None:
        """The one place where optimism would be a security problem of its own."""
        document = read_repo_file("SECURITY.md")

        assert "Sanitizer 尚未完整实现" in document
        assert "原样落库" in document

    def test_contributing_document_forbids_rewriting_published_revisions(self) -> None:
        document = read_repo_file("CONTRIBUTING.md")

        assert "已发布 revision 不允许改写" in document
        assert "必须新增 revision" in document
        assert "绝不 `alembic downgrade`" in document

    def test_contributing_documents_only_commands_that_exist(self) -> None:
        """A contributor guide that lists a command the repository does not have is
        worse than one that lists nothing: it costs a newcomer their first hour."""
        document = read_repo_file("CONTRIBUTING.md")

        for command in ("pytest", "ruff check .", "ruff format --check .", "mypy aer/"):
            assert command in document, f"{command} should be documented"
