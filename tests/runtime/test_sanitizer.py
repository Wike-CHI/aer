"""The Sanitizer milestone (agent.md #39, TASKS #11): Agent → Sanitizer → Storage.

The acceptance test of the milestone is the first one here, stated exactly as the
brief states it: a ``Bearer sk-...`` token handed to a tool hook must not appear
in the database. Everything else is the rule's mechanics -- key-based redaction,
shape-based redaction, depth and length caps with explicit markers, idempotence --
and the guard that ordinary trace data passes through untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from aer import AER
from aer.experience.candidate import ExperienceCandidate
from aer.experience.provider import CallableDistillationProvider
from aer.runtime.sanitization import (
    JSON_MAX_DEPTH,
    JSON_STRING_MAX_LENGTH,
    REDACTED,
    is_secret_key,
    redact,
    sanitize_json,
)

SECRET = "sk-abcdefghijklmnop0123456789"
BEARER = f"Bearer {SECRET}"
# Built at runtime: the repository-hygiene scan greps sources for PEM headers, so
# the test file itself must never contain the literal.
_PEM_BLOCK = (
    "-----BEGIN OPEN" + "SSH PRIVATE KEY-----\n"
    "abc123notarealkey\n"
    "-----END OPEN" + "SSH PRIVATE KEY-----"
)


def _dump_events_text(db_path) -> str:
    """Every text column of every event row, concatenated: the leak-detector."""
    con = sqlite3.connect(str(db_path))
    try:
        parts: list[str] = []
        for (payload,) in con.execute("SELECT input_json FROM events WHERE input_json IS NOT NULL"):
            parts.append(payload)
        for (payload,) in con.execute(
            "SELECT output_json FROM events WHERE output_json IS NOT NULL"
        ):
            parts.append(payload)
        for (payload,) in con.execute(
            "SELECT metadata_json FROM events WHERE metadata_json IS NOT NULL"
        ):
            parts.append(payload)
        return "\n".join(parts)
    finally:
        con.close()


#: Every text column of the experiences table -- the same leak-detector, one level
#: up: the Sanitizer must also cover what a distillation provider writes into the
#: knowledge base, not only what an agent writes into the trace.
_EXPERIENCE_TEXT_COLUMNS = (
    "domain, title, problem, symptoms_json, root_cause, solution, "
    "failed_attempts_json, workflow_json, avoid_json, dedup_key, metadata_json"
)


def _dump_experiences_text(db_path) -> str:
    con = sqlite3.connect(str(db_path))
    try:
        parts: list[str] = []
        for row in con.execute(f"SELECT {_EXPERIENCE_TEXT_COLUMNS} FROM experiences"):
            parts.extend(str(value) for value in row if value is not None)
        return "\n".join(parts)
    finally:
        con.close()


class TestAcceptance:
    """TASKS #11: `Bearer sk-xxxx` 数据库中不得出现原值。"""

    def test_bearer_token_in_tool_input_never_reaches_the_database(
        self, data_dir, tmp_path
    ) -> None:
        with AER(data_dir) as aer:
            run = aer.start_run(task="probe", task_type="sanitizer")
            with run.tool("shell.deploy", input={"command": f"deploy --token {BEARER}"}) as tool:
                tool.set_result({"status": "ok"})

        events_text = _dump_events_text(tmp_path / "data" / "aer.db")
        assert SECRET not in events_text
        assert BEARER not in events_text
        assert "Bearer [REDACTED]" in events_text

    def test_bearer_token_in_tool_result_never_reaches_the_database(
        self, data_dir, tmp_path
    ) -> None:
        with AER(data_dir) as aer:
            run = aer.start_run(task="probe", task_type="sanitizer")
            with run.tool("shell.login") as tool:
                tool.set_result({"stdout": f"authenticated with {BEARER}"})

        events_text = _dump_events_text(tmp_path / "data" / "aer.db")
        assert SECRET not in events_text

    def test_secret_keyed_structures_are_redacted_whole(self, data_dir, tmp_path) -> None:
        with AER(data_dir) as aer:
            run = aer.start_run(task="probe", task_type="sanitizer")
            with run.tool(
                "wp.update",
                input={"page_id": 123, "credentials": {"user": "bot", "password": "hunter2"}},
            ) as tool:
                tool.set_result({"ok": True})

        events_text = _dump_events_text(tmp_path / "data" / "aer.db")
        assert "hunter2" not in events_text
        assert '"credentials":"[REDACTED]"' in events_text

    def test_run_metadata_is_sanitized_at_start(self, data_dir) -> None:
        with AER(data_dir) as aer:
            run = aer.start_run(
                task="probe",
                task_type="sanitizer",
                metadata={"config": {"api_key": SECRET}, "note": "plain"},
            )
            stored = aer.get_run(run.run_id)

        assert stored is not None
        assert stored.metadata["config"]["api_key"] == REDACTED
        assert stored.metadata["note"] == "plain"

    def test_error_metadata_is_sanitized(self, data_dir) -> None:
        with AER(data_dir) as aer:
            run = aer.start_run(task="probe", task_type="sanitizer")
            run.error(
                RuntimeError("boom"),
                metadata={"request": {"headers": {"authorization": BEARER}}},
            )

        error = aer.errors.get_by_run(run.run_id)[0]
        assert SECRET not in str(error.metadata)
        assert error.metadata["request"]["headers"]["authorization"] == REDACTED


class TestDistillationAcceptance:
    """The Sanitizer's other write path: provider-produced experience text.

    A distillation provider is arbitrary code reading a run that contains whatever
    the agent handled. Its prose lands verbatim in the ``experiences`` table and in
    the ``dedup_key`` fingerprint, both of which outlive the run -- so
    ``sanitize_candidate`` runs at the distiller boundary, before either exists.
    """

    def test_provider_produced_text_never_reaches_the_experiences_table(
        self, data_dir, tmp_path
    ) -> None:
        def leaky_distill(evidence):
            del evidence
            return ExperienceCandidate(
                domain="wordpress",
                title=f"403 fix using {BEARER}",
                problem="the page update was rejected",
                symptoms=(f"re-sent the request with {BEARER}",),
                root_cause=f"the api_key={SECRET} had expired",
                solution=f"rotate api_key to {BEARER}",
                metadata={"request": {"headers": {"authorization": BEARER}}},
            )

        provider = CallableDistillationProvider(leaky_distill, name="leaky")
        with AER(data_dir, distillation_provider=provider) as aer:
            run = aer.start_run(task="probe", task_type="sanitizer")
            run.fail()

            experience = aer.distill_run(run.run_id, explicit_high_value=True)

        assert experience is not None
        # On a FAILURE the solution is preserved as a hypothesis in metadata, so the
        # redaction must survive that move too.
        text = _dump_experiences_text(tmp_path / "data" / "aer.db")
        assert SECRET not in text
        assert "Bearer [REDACTED]" in text
        assert experience.metadata["request"]["headers"]["authorization"] == REDACTED
        hypothesis = experience.metadata["provider_solution_hypothesis"]
        assert isinstance(hypothesis, str)
        assert SECRET not in hypothesis
        assert "Bearer [REDACTED]" in hypothesis


class TestSecretShapes:
    """TASKS #11's default-secret list, shape by shape."""

    @pytest.mark.parametrize(
        "sample",
        [
            "Authorization: Basic dXNlcjpwYXNz",
            BEARER,
            "api_key=abcdef1234567890",
            "password: hunter2hunter2",
            "Cookie: session=abcdef123456",
            _PEM_BLOCK,
            "postgres://deploy:hunter2@db.internal:5432/app",
            "client_secret = shhh-1234",
            "access_token=aaaa1111bbbb2222",
            "refresh_token=cccc3333dddd4444",
        ],
    )
    def test_default_secret_shapes_are_redacted(self, sample: str) -> None:
        redacted = redact(sample)

        assert sample != redacted
        assert "[REDACTED]" in redacted

    def test_database_url_inline_password(self) -> None:
        assert redact("mysql://root:s3cret@host/db") == "mysql://root:[REDACTED]@host/db"

    def test_ordinary_prose_is_untouched(self) -> None:
        text = "Update the page, then verify the H1. token_count=3 author=alice"

        assert redact(text) == text


class TestSanitizeJson:
    def test_plain_payloads_pass_through_unchanged(self) -> None:
        payload = {
            "tool": "wp.update_page",
            "arguments": {"page_id": 123, "title": "Best 产品"},
            "attempt": 2,
            "ok": True,
            "score": 0.95,
            "nothing": None,
        }

        assert sanitize_json(payload) == payload

    def test_secret_key_replaces_the_whole_subtree(self) -> None:
        payload = {"api_key": {"nested": {"deeper": SECRET}}, "keep": "me"}

        assert sanitize_json(payload) == {"api_key": REDACTED, "keep": "me"}

    def test_long_strings_are_cut_with_an_explicit_marker(self) -> None:
        payload = {"log": "x" * (JSON_STRING_MAX_LENGTH + 500)}

        cleaned = sanitize_json(payload)

        assert len(cleaned["log"]) <= JSON_STRING_MAX_LENGTH
        assert "truncated 500 chars" in cleaned["log"]

    def test_deep_structures_are_cut_with_an_explicit_marker(self) -> None:
        payload: dict = {"value": "leaf"}
        for _ in range(JSON_MAX_DEPTH + 3):
            payload = {"level": payload}

        cleaned = sanitize_json(payload)

        text = str(cleaned)
        assert f"deeper than {JSON_MAX_DEPTH} levels" in text
        assert "leaf" not in text

    def test_sanitize_is_idempotent(self) -> None:
        payload = {
            "token": SECRET,
            "nested": [BEARER, "x" * 5000],
            "safe": {"a": 1},
        }

        once = sanitize_json(payload)

        assert sanitize_json(once) == once

    def test_secret_strings_inside_lists_are_redacted(self) -> None:
        payload = {"outputs": ["all good", f"auth: {BEARER}", "done"]}

        cleaned = sanitize_json(payload)

        assert SECRET not in str(cleaned)
        assert cleaned["outputs"][0] == "all good"


class TestKeyClassification:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "api_key",
            "apiKey",
            "access_token",
            "refreshToken",
            "client_secret",
            "password",
            "session_cookie",
            "private_key",
            "signing_key",
            "credentials",
        ],
    )
    def test_credential_keys_are_recognised(self, key: str) -> None:
        assert is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "author",
            "token_count",
            "tool_use_id",
            "page_id",
            "title",
            "description",
            "status",
            "result",
            "error_message",
        ],
    )
    def test_ordinary_keys_are_left_alone(self, key: str) -> None:
        assert is_secret_key(key) is False
