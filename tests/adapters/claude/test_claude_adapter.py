"""Claude Code adapter tests: mapping, lifecycle, and the refusals that matter.

The payloads in ``fixtures/`` are **captured** -- verbatim deliveries recorded by
``scripts/probe_claude_hooks.py`` against the installed CLI (2.1.310, print mode,
2026-10-26), with ``MANIFEST.json`` declaring their provenance. The tests assert
the mapping against those real payloads, because the fixture/provenance split is
the discipline that turned the Codex adapter's D-097 from a risk into a procedure:
a mapping that has never met a real event must never be reported as observed.

The capture history matters as much as the fixtures. The first probe run
contradicted the vocabulary extracted from the binary's string table -- the CLI's
own validator rejected ``Teardown`` and ``PostToolUseBatch`` -- and those
rejections are pinned as tests here (D-101), so a "cleanup" can never quietly
re-add them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aer import AER, RunStatus
from aer.adapter.claude import (
    ADAPTER_NAME,
    CLAUDE_PROVIDER,
    VERIFIED_CLAUDE_VERSION,
    ClaudeCodeAdapter,
    ClaudeHookEvent,
    EvidenceLevel,
    default_coverage,
    event_name,
    external_event_id,
    mapping,
    session_id,
    task_summary,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType
from tests.adapters.conftest import adapter_runtime  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, object]:
    """Load one captured payload by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def manifest() -> dict[str, object]:
    """The fixture provenance manifest."""
    return json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


SESSION_START = load_fixture("print_session_start.json")
PROMPT_ONE = load_fixture("print_user_prompt_submit.json")
TOOL_CALL = load_fixture("print_pre_tool_use_bash.json")
TOOL_RESULT = load_fixture("print_post_tool_use_bash.json")
TOOL_FAILURE = load_fixture("print_post_tool_use_failure_read.json")
STOP = load_fixture("print_stop.json")
SESSION_END = load_fixture("print_session_end.json")

PROMPT_TWO = {
    **PROMPT_ONE,
    "prompt": "Now update the meta description",
    "prompt_id": "11111111-2222-3333-4444-555555555555",
}


def payload(event: ClaudeHookEvent, session: str = "sess-1", **extra: object) -> dict[str, object]:
    """A contract fixture: the common envelope every hook payload carries."""
    return {
        "session_id": session,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "D:\\work",
        "permission_mode": "default",
        "hook_event_name": event.value,
        **extra,
    }


@pytest.fixture
def claude_adapter() -> ClaudeCodeAdapter:
    """An adapter with a fixed identity, so tests never depend on the host's CLI."""
    return ClaudeCodeAdapter(agent_version=VERIFIED_CLAUDE_VERSION, detect_version=False)


class TestFixtureProvenance:
    """Fixtures come from real payloads, and say so."""

    def test_every_fixture_is_declared_in_the_manifest(self) -> None:
        declared = set(manifest()["fixtures"])
        present = {path.name for path in FIXTURES.glob("*.json")} - {"MANIFEST.json"}

        assert present == declared

    def test_every_fixture_is_marked_captured(self) -> None:
        for name, kind in manifest()["fixtures"].items():
            assert kind == "captured", f"{name} must not claim more than it is"

    def test_the_manifest_names_the_cli_version_and_probe(self) -> None:
        assert manifest()["claude_version"] == VERIFIED_CLAUDE_VERSION
        assert "probe_claude_hooks" in manifest()["source"]


class TestMappingAgainstCapturedPayloads:
    """The mapping is checked against what the CLI actually sent, not a wish."""

    def test_session_start_carries_no_task(self) -> None:
        """D-091 confirmed by capture: SessionStart cannot open a run."""
        assert event_name(SESSION_START) is ClaudeHookEvent.SESSION_START
        assert "prompt" not in SESSION_START
        assert session_id(SESSION_START)

    def test_user_prompt_submit_carries_prompt_and_prompt_id(self) -> None:
        assert event_name(PROMPT_ONE) is ClaudeHookEvent.USER_PROMPT_SUBMIT
        assert isinstance(PROMPT_ONE.get("prompt"), str)
        assert isinstance(PROMPT_ONE.get("prompt_id"), str)

    def test_pre_tool_use_is_a_structured_tool_call(self) -> None:
        assert TOOL_CALL["tool_name"] == "Bash"
        assert isinstance(TOOL_CALL["tool_input"], dict)
        assert isinstance(TOOL_CALL["tool_use_id"], str)

    def test_post_tool_use_response_states_no_outcome(self) -> None:
        """D-102: the captured Bash response has no success/failure verdict."""
        response = TOOL_RESULT["tool_response"]
        assert isinstance(response, dict)
        for key in ("success", "error", "is_error", "exit_code"):
            assert key not in response, f"{key} must not be invented from prose"

    def test_post_tool_use_failure_is_the_structured_failure_channel(self) -> None:
        """D-102: failures arrive as their own event with a structured error."""
        assert event_name(TOOL_FAILURE) is ClaudeHookEvent.POST_TOOL_USE_FAILURE
        assert isinstance(TOOL_FAILURE["error"], str)
        assert "tool_response" not in TOOL_FAILURE

    def test_stop_carries_no_outcome_and_no_stop_reason(self) -> None:
        """D-098 parallel: Stop is turn-level; it even fired on a failed provider call."""
        assert event_name(STOP) is ClaudeHookEvent.STOP
        assert "stop_reason" not in STOP

    def test_session_end_reason_is_an_exit_cause_not_an_outcome(self) -> None:
        """D-099 parallel: this captured SessionEnd (reason='other') ended a turn
        whose last assistant message was a successful reply."""
        assert event_name(SESSION_END) is ClaudeHookEvent.SESSION_END
        assert SESSION_END["reason"] == "other"
        assert (
            "succeeded" in STOP["last_assistant_message"]
            or "ok" in (STOP["last_assistant_message"]).lower()
        )


class TestMappingVocabulary:
    """The closed vocabulary confirmed by the CLI's own validator (D-101)."""

    def test_every_event_in_the_enum_parses(self) -> None:
        for event in ClaudeHookEvent:
            assert event_name(payload(event)) is event

    def test_teardown_is_not_a_hook_event(self) -> None:
        """D-101, pinned: the string table named it, the validator rejected it."""
        with pytest.raises(UnsupportedAdapterEvent, match="does not know"):
            event_name({"hook_event_name": "Teardown", "session_id": "s"})
        assert not hasattr(ClaudeHookEvent, "TEARDOWN")

    def test_post_tool_batch_is_the_valid_spelling(self) -> None:
        """D-101, pinned: the plausible string-table spelling is rejected."""
        with pytest.raises(UnsupportedAdapterEvent, match="does not know"):
            event_name({"hook_event_name": "PostToolUseBatch", "session_id": "s"})
        assert (
            event_name({"hook_event_name": "PostToolBatch", "session_id": "s"})
            is ClaudeHookEvent.POST_TOOL_BATCH
        )

    def test_an_unknown_hook_event_is_refused(self) -> None:
        with pytest.raises(UnsupportedAdapterEvent, match="does not know"):
            event_name({"hook_event_name": "SomethingNew", "session_id": "s"})

    def test_a_payload_without_an_event_name_is_not_a_hook_delivery(self) -> None:
        with pytest.raises(AdapterProtocolError, match="hook_event_name"):
            event_name({"session_id": "s"})

    def test_a_payload_without_a_session_id_cannot_be_placed(self) -> None:
        with pytest.raises(AdapterProtocolError, match="session_id"):
            session_id({"hook_event_name": "SessionStart"})

    def test_only_the_three_tool_events_produce_envelopes(self) -> None:
        assert set(mapping.ENVELOPE_FOR) == {
            ClaudeHookEvent.PRE_TOOL_USE,
            ClaudeHookEvent.POST_TOOL_USE,
            ClaudeHookEvent.POST_TOOL_USE_FAILURE,
        }

    def test_only_session_end_closes_a_run(self) -> None:
        """D-101: SessionEnd is the single terminal authority."""
        assert mapping.closes_run(ClaudeHookEvent.SESSION_END) is True
        assert mapping.closes_run(ClaudeHookEvent.STOP) is False
        assert mapping.closes_run(ClaudeHookEvent.USER_PROMPT_SUBMIT) is False


class TestTaskSummary:
    def test_credentials_are_redacted_before_the_task_field(self) -> None:
        summary = task_summary("deploy with Bearer sk-abcdef1234567890 please")

        assert "sk-abcdef1234567890" not in summary
        assert "[REDACTED" in summary

    def test_multiline_prompts_collapse_to_the_first_line(self) -> None:
        assert task_summary("fix the H1\n\nmore context here") == "fix the H1"

    def test_long_prompts_are_cut_with_a_marker(self) -> None:
        summary = task_summary("x" * 500)

        assert len(summary) <= 200
        assert summary.endswith("…")

    def test_an_empty_prompt_gets_an_explicit_placeholder(self) -> None:
        assert task_summary("   ") == "Claude Code session (empty prompt)"


class TestExternalEventId:
    def test_the_same_delivery_always_hashes_the_same(self) -> None:
        first = external_event_id(ClaudeHookEvent.PRE_TOOL_USE, TOOL_CALL)
        second = external_event_id(ClaudeHookEvent.PRE_TOOL_USE, dict(TOOL_CALL))

        assert first == second
        assert first.startswith("claude-")

    def test_different_deliveries_never_merge(self) -> None:
        other = dict(TOOL_CALL, tool_input={"command": "wp post update 456"})

        assert external_event_id(ClaudeHookEvent.PRE_TOOL_USE, TOOL_CALL) != (
            external_event_id(ClaudeHookEvent.PRE_TOOL_USE, other)
        )

    def test_a_call_and_its_result_are_distinct_events(self) -> None:
        assert external_event_id(ClaudeHookEvent.PRE_TOOL_USE, TOOL_CALL) != (
            external_event_id(ClaudeHookEvent.POST_TOOL_USE, TOOL_RESULT)
        )

    def test_a_call_and_its_failure_are_distinct_events(self) -> None:
        """Same tool_use_id, different event: never collapse into one."""
        assert external_event_id(ClaudeHookEvent.PRE_TOOL_USE, TOOL_CALL) != (
            external_event_id(ClaudeHookEvent.POST_TOOL_USE_FAILURE, TOOL_FAILURE)
        )


class TestToolEnvelopes:
    def test_pre_tool_use_becomes_a_tool_call(self, claude_adapter: ClaudeCodeAdapter) -> None:
        (envelope,) = claude_adapter.handle_event(TOOL_CALL)

        assert envelope.event_type is EventType.TOOL_CALL
        assert envelope.action is not None
        assert envelope.action.tool_name == "Bash"
        assert "wp" in (envelope.action.input_summary or "") or "echo" in (
            envelope.action.input_summary or ""
        )
        assert envelope.external_event_id is not None

    def test_post_tool_use_states_no_verdict(self, claude_adapter: ClaudeCodeAdapter) -> None:
        """D-102: no outcome field, so no success claim -- not even True."""
        (envelope,) = claude_adapter.handle_event(TOOL_RESULT)

        assert envelope.event_type is EventType.TOOL_RESULT
        assert envelope.payload["tool_use_id"] == TOOL_RESULT["tool_use_id"]
        assert "success" not in envelope.payload
        assert "error" not in envelope.payload

    def test_post_tool_use_failure_carries_the_structured_error(
        self, claude_adapter: ClaudeCodeAdapter
    ) -> None:
        """D-102: the dedicated failure channel maps to a failing TOOL_RESULT."""
        (envelope,) = claude_adapter.handle_event(TOOL_FAILURE)

        assert envelope.event_type is EventType.TOOL_RESULT
        assert envelope.payload["success"] is False
        assert envelope.payload["error"]["error_type"] == "claude.ToolFailure"
        assert "File does not exist" in envelope.payload["error"]["message"]

    def test_non_tool_events_produce_no_envelopes(self, claude_adapter: ClaudeCodeAdapter) -> None:
        assert claude_adapter.handle_event(SESSION_START) == ()
        assert claude_adapter.handle_event(STOP) == ()
        assert claude_adapter.handle_event(SESSION_END) == ()


class TestAdapter:
    def test_identity_names_who_it_speaks_for(self, claude_adapter: ClaudeCodeAdapter) -> None:
        identity = claude_adapter.identity()

        assert identity.provider == CLAUDE_PROVIDER == "anthropic"
        assert identity.agent_name == "claude-code"
        assert identity.adapter_name == ADAPTER_NAME
        assert identity.agent_version == VERIFIED_CLAUDE_VERSION

    def test_capabilities_are_conservative(self, claude_adapter: ClaudeCodeAdapter) -> None:
        """Adoption is the dangerous claim, and no Claude Code hook reports it."""
        capabilities = claude_adapter.capabilities()

        assert capabilities.tool_events is True
        assert capabilities.session_linkage is True
        assert capabilities.explicit_adoption_signal is False
        assert capabilities.explicit_utility_signal is False
        assert capabilities.external_verification is False

    def test_start_builds_the_task_from_the_prompt(self, claude_adapter: ClaudeCodeAdapter) -> None:
        request = claude_adapter.start(PROMPT_ONE)

        assert request.external_session_id == PROMPT_ONE["session_id"]
        assert request.task == str(PROMPT_ONE["prompt"]).strip().splitlines()[0]
        assert request.task_type == PROMPT_ONE["cwd"]

    def test_start_without_a_prompt_degrades_to_a_placeholder(
        self, claude_adapter: ClaudeCodeAdapter
    ) -> None:
        request = claude_adapter.start(payload(ClaudeHookEvent.USER_PROMPT_SUBMIT, prompt=""))

        assert "task not stated" in request.task

    def test_finish_is_always_inconclusive(self, claude_adapter: ClaudeCodeAdapter) -> None:
        """No Claude Code hook declares a task outcome (D-100)."""
        for closing in (SESSION_END, PROMPT_TWO):
            request = claude_adapter.finish(closing)
            assert request.status is RunStatus.INCONCLUSIVE

    def test_finish_refuses_a_stop_payload(self, claude_adapter: ClaudeCodeAdapter) -> None:
        """D-098 parallel: Stop is turn-level and never terminates a run."""
        with pytest.raises(AdapterProtocolError, match="Stop"):
            claude_adapter.finish(STOP)


class TestLifecycle:
    """End to end through the real ingestor: one run per turn, never per session."""

    def test_a_full_turn_records_and_closes_inconclusive(
        self,
        adapter_runtime: AER,  # noqa: F811
        claude_adapter: ClaudeCodeAdapter,
    ) -> None:
        ingestor = adapter_runtime.adapter_ingestor

        handle = ingestor.open(claude_adapter, PROMPT_ONE)
        assert handle.outcome.value == "STARTED"
        for delivery in (TOOL_CALL, TOOL_RESULT):
            result = ingestor.ingest(handle, delivery)
            assert result.applied
        run = ingestor.close(ingestor.open(claude_adapter, SESSION_END), SESSION_END)

        assert run is not None
        assert run.status is RunStatus.INCONCLUSIVE
        event_types = [e.event_type.value for e in adapter_runtime.get_events(run.id)]
        assert event_types == [
            "TASK_START",
            "TOOL_CALL",
            "TOOL_RESULT",
            "TASK_END",
        ]

    def test_a_failing_tool_records_error_then_result(
        self,
        adapter_runtime: AER,  # noqa: F811
        claude_adapter: ClaudeCodeAdapter,
    ) -> None:
        """D-102 end to end: PostToolUseFailure becomes the ERROR+TOOL_RESULT pair."""
        ingestor = adapter_runtime.adapter_ingestor

        handle = ingestor.open(claude_adapter, PROMPT_ONE)
        ingestor.ingest(handle, TOOL_CALL)
        ingestor.ingest(handle, TOOL_FAILURE)
        run = ingestor.close(ingestor.open(claude_adapter, SESSION_END), SESSION_END)

        assert run is not None
        event_types = [e.event_type.value for e in adapter_runtime.get_events(run.id)]
        assert event_types == [
            "TASK_START",
            "TOOL_CALL",
            "ERROR",
            "TOOL_RESULT",
            "TASK_END",
        ]

    def test_a_second_prompt_closes_the_previous_turn(
        self,
        adapter_runtime: AER,  # noqa: F811
        claude_adapter: ClaudeCodeAdapter,
    ) -> None:
        """A session outlives its tasks; an AER run must not."""
        ingestor = adapter_runtime.adapter_ingestor

        # The hook's turn-boundary sequence: the new prompt closes the previous run
        # (INCONCLUSIVE) and only then does the new episode open.
        first = ingestor.open(claude_adapter, PROMPT_ONE)
        ingestor.close(ingestor.open(claude_adapter, PROMPT_TWO), PROMPT_TWO)
        reopened = ingestor.reopen(claude_adapter, PROMPT_TWO)

        assert reopened.outcome.value == "REOPENED"
        assert reopened.run_id != first.run_id

        first_run = adapter_runtime.get_run(first.run_id)
        assert first_run is not None
        assert first_run.status is RunStatus.INCONCLUSIVE
        assert first_run.metadata["adapter_close_reason"].startswith("new prompt")

        second_run = adapter_runtime.get_run(reopened.run_id)
        assert second_run is not None
        assert second_run.status is RunStatus.RUNNING
        assert second_run.task_description == "Now update the meta description"

    def test_a_redelivered_tool_hook_is_a_no_op(
        self,
        adapter_runtime: AER,  # noqa: F811
        claude_adapter: ClaudeCodeAdapter,
    ) -> None:
        ingestor = adapter_runtime.adapter_ingestor
        handle = ingestor.open(claude_adapter, PROMPT_ONE)

        first = ingestor.ingest(handle, TOOL_CALL)
        second = ingestor.ingest(handle, dict(TOOL_CALL))

        assert first.applied
        assert not second.applied
        assert second.outcome.value == "DUPLICATE"


class TestCoverage:
    def test_captured_cells_match_the_probe_matrix(self) -> None:
        """Nine events have real payloads; the rest are validator-confirmed only."""
        coverage = default_coverage()
        print_cells = coverage.modes[_print_mode()]

        captured = {event for event, cell in print_cells.items() if cell.observed}
        assert captured == {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "SessionEnd",
            "InstructionsLoaded",
            "CwdChanged",
        }
        rest = set(print_cells) - captured
        assert rest, "the vocabulary-only cells must not be empty"
        assert all(print_cells[event].level is EvidenceLevel.VOCABULARY for event in rest)

    def test_every_enum_event_has_a_matrix_cell(self) -> None:
        coverage = default_coverage()
        cells = coverage.modes[_print_mode()]

        assert set(cells) == {event.value for event in ClaudeHookEvent}


def _print_mode():
    from aer.adapter.claude.coverage import ClaudeMode

    return ClaudeMode.PRINT


class TestHookCommand:
    """The process Claude Code actually runs: stdin JSON in, exit code out."""

    @staticmethod
    def _deliver(
        data_dir: Path, body: dict[str, object], *extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "AER_DATA_DIR": str(data_dir),
            "AER_ENV": "development",
        }
        return subprocess.run(
            [sys.executable, "-m", "aer.adapter.claude.hook", *extra],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            timeout=120,
            check=False,
        )

    def test_a_full_turn_via_the_hook_command(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        deliveries = [SESSION_START, PROMPT_ONE, TOOL_CALL, TOOL_RESULT, STOP, SESSION_END]
        for body in deliveries:
            completed = self._deliver(data, body)
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == "", (
                "stdout is a protocol surface for Claude Code; diagnostics go to stderr"
            )

        with AER(data) as runtime:
            runs = runtime.list_runs()
            assert len(runs) == 1
            run = runs[0]
            assert run.task_description == str(PROMPT_ONE["prompt"])
            assert run.status is RunStatus.INCONCLUSIVE
            assert run.agent_name == "claude-code"
            assert run.metadata["provider"] == CLAUDE_PROVIDER
            assert run.metadata["adapter"]["name"] == ADAPTER_NAME
            event_types = [e.event_type.value for e in runtime.get_events(run.id)]
            assert event_types == [
                "TASK_START",
                "TOOL_CALL",
                "TOOL_RESULT",
                "TASK_END",
            ]

            session = runtime.find_adapter_session(CLAUDE_PROVIDER, str(PROMPT_ONE["session_id"]))
            assert session is not None and session.aer_run_id == run.id

    def test_two_prompts_produce_two_runs(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        for body in (PROMPT_ONE, TOOL_CALL, PROMPT_TWO, SESSION_END):
            completed = self._deliver(data, body)
            assert completed.returncode == 0, completed.stderr

        with AER(data) as runtime:
            runs = sorted(runtime.list_runs(), key=lambda r: r.started_at)
            assert len(runs) == 2
            assert runs[0].task_description == str(PROMPT_ONE["prompt"])
            assert runs[0].status is RunStatus.INCONCLUSIVE
            assert runs[1].task_description == "Now update the meta description"
            assert runs[1].status is RunStatus.INCONCLUSIVE

    def test_the_shipped_coverage_can_be_printed(self, tmp_path: Path) -> None:
        completed = self._deliver(tmp_path / "data", SESSION_START, "--print-coverage")

        assert completed.returncode == 0
        assert VERIFIED_CLAUDE_VERSION in completed.stdout
        assert "CAPTURED" in completed.stdout
        assert "VOCABULARY" in completed.stdout
        assert "print mode" in completed.stdout
        assert "interactive mode" in completed.stdout

    def test_an_unknown_event_fails_loudly_and_writes_nothing(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        completed = self._deliver(data, {"hook_event_name": "SomethingNew", "session_id": "s"})

        assert completed.returncode != 0
        assert "does not know" in completed.stderr
        with AER(data) as runtime:
            assert runtime.list_runs() == []

    def test_garbage_stdin_fails_loudly(self, tmp_path: Path) -> None:
        env = {**os.environ, "AER_DATA_DIR": str(tmp_path / "data")}
        completed = subprocess.run(
            [sys.executable, "-m", "aer.adapter.claude.hook"],
            input="not json at all",
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            timeout=120,
            check=False,
        )

        assert completed.returncode != 0
        assert "could not read" in completed.stderr

    def test_capture_mode_writes_raw_payloads_for_the_probe(self, tmp_path: Path) -> None:
        capture = tmp_path / "capture"
        completed = self._deliver(tmp_path / "data", PROMPT_ONE, "--capture", str(capture))

        assert completed.returncode == 0
        written = list(capture.glob("UserPromptSubmit-*.json"))
        assert len(written) == 1
        assert '"prompt"' in written[0].read_text(encoding="utf-8")
