"""Codex adapter tests: mapping, lifecycle, and the refusals that matter.

Three groups, and the third is the point of the milestone:

* **mapping** -- captured payloads become the protocol values they should, and an
  unknown payload shape fails loudly instead of being mapped into something plausible;
* **lifecycle** -- one run per Codex session, resumed on repeat hooks, finished only by
  ``SessionEnd``;
* **refusals** -- the four things an integration is most tempted to invent. A tool
  succeeding is not an experience being adopted; a failed tool followed by a successful
  one is not a recovery; a Codex message saying the tests pass is not a verification;
  and a hook that ran is not a hook whose output reached the model's context. Each of
  those is asserted as an *absence*, because the failure mode is a plausible-looking row
  appearing where nothing was observed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aer import (
    AER,
    AdapterCapabilityError,
    ExperienceKind,
    GenericAgentAdapter,
    HttpStatusVerifier,
    RunStatus,
    UsageSignal,
    UtilityLabel,
    VerificationContext,
)
from aer.adapter.codex import (
    ADAPTER_NAME,
    CODEX_PROVIDER,
    VERIFIED_CODEX_VERSION,
    CodexAdapter,
    CodexHookEvent,
    CodexMode,
    SessionCompleteness,
    default_coverage,
    detect_codex_version,
    event_name,
    external_event_id,
    finish_status,
    mapping,
    session_id,
    task_summary,
)
from aer.exceptions import AdapterProtocolError, UnsupportedAdapterEvent
from aer.runtime.enums import EventType
from tests.adapters.codex.conftest import FIXTURES, load_fixture, manifest

SESSION_START = "exec_session_start.json"
PROMPT_SUBMIT = "exec_user_prompt_submit.json"
SESSION_END = "exec_session_end.json"
TOOL_CALL = "exec_pre_tool_use_bash.json"
TOOL_PATCH = "exec_pre_tool_use_apply_patch.json"
TOOL_FAILURE = "exec_post_tool_use_bash_failure.json"
TOOL_SUCCESS = "exec_post_tool_use_bash_success.json"
TOOL_PATCH_RESULT = "exec_post_tool_use_apply_patch_patch.json"
STOP = "exec_stop.json"


class TestFixtureProvenance:
    """Section 45: fixtures come from real payloads, and say so when they do not."""

    def test_every_fixture_is_declared_in_the_manifest(self) -> None:
        declared = set(manifest()["fixtures"])
        present = {path.name for path in FIXTURES.glob("*.json")} - {"MANIFEST.json"}

        assert present == declared

    def test_captured_fixtures_are_marked_captured(self) -> None:
        declared = manifest()["fixtures"]

        captured = (
            SESSION_START,
            PROMPT_SUBMIT,
            SESSION_END,
            TOOL_CALL,
            TOOL_PATCH,
            TOOL_FAILURE,
            TOOL_SUCCESS,
            TOOL_PATCH_RESULT,
            STOP,
        )
        for name in captured:
            assert declared[name]["provenance"] == "captured"

    def test_no_contract_fixture_survives(self) -> None:
        """Real payloads arrived, so every hand-written one was deleted (section 6).

        The guard is the *absence* of contract fixtures rather than an assertion about a
        particular file: if a future shape cannot be captured, adding a contract fixture
        has to be a visible decision that fails here first.
        """
        declared = manifest()["fixtures"]

        assert all(entry["provenance"] == "captured" for entry in declared.values())
        assert not list(FIXTURES.glob("contract_*.json"))

    def test_the_manifest_names_the_version_it_was_captured_against(self) -> None:
        assert manifest()["codex_version"] == VERIFIED_CODEX_VERSION


class TestCapturedPayloads:
    def test_session_start_is_a_session_fact_not_a_run(self, codex_adapter: CodexAdapter) -> None:
        """It carries no task, so nothing can be described and no run is opened (D-091)."""
        payload = load_fixture(SESSION_START)

        assert event_name(payload) is CodexHookEvent.SESSION_START
        assert codex_adapter.handle_event(payload) == ()
        assert "prompt" not in payload

    def test_the_prompt_payload_is_the_one_that_describes_the_task(
        self, codex_adapter: CodexAdapter
    ) -> None:
        payload = load_fixture(PROMPT_SUBMIT)

        request = codex_adapter.start(payload)

        assert request.external_session_id == payload["session_id"]
        assert request.task == task_summary(str(payload["prompt"]))
        assert request.metadata["codex"]["turn_id"] == payload["turn_id"]

    def test_session_end_states_no_outcome(self, codex_adapter: CodexAdapter) -> None:
        """Section 10: a session ending is not a task succeeding, and not a failure either.

        ``reason: "other"`` was the value on **every** captured session, including ones
        that completed their work, so the adapter has to close the run without claiming
        anything. ``INCONCLUSIVE`` is that status: the run is over, the outcome is
        unstated, and verification can still settle it (D-100).
        """
        payload = load_fixture(SESSION_END)

        assert finish_status(payload) is None
        request = codex_adapter.finish(payload)
        assert request.status is RunStatus.INCONCLUSIVE
        assert request.metadata["codex_outcome_stated"] is False

    def test_a_stated_completion_is_believed(self, codex_adapter: CodexAdapter) -> None:
        """``RunStatus`` is the agent's declaration; a stated one is recorded as such."""
        payload = {**load_fixture(SESSION_END), "reason": "complete"}

        request = codex_adapter.finish(payload)

        assert request.status is RunStatus.SUCCESS
        assert request.metadata["codex_outcome_stated"] is True

    def test_the_identity_names_the_agent_and_the_adapter(
        self, codex_adapter: CodexAdapter
    ) -> None:
        identity = codex_adapter.identity()

        assert identity.provider == "openai"
        assert identity.agent_name == "codex"
        assert identity.agent_version == VERIFIED_CODEX_VERSION
        assert identity.adapter_name == ADAPTER_NAME
        assert identity.model is None  # never inferred (section 4)


class TestTheHookEntryPoint:
    """The real command Codex runs, driven with the real captured payloads."""

    @staticmethod
    def _deliver(
        payload_name: str, data_dir: Path, *extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "AER_DATA_DIR": str(data_dir),
            "AER_ENV": "development",
        }
        return subprocess.run(
            [sys.executable, "-m", "aer.adapter.codex.hook", *extra],
            input=(FIXTURES / payload_name).read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            timeout=120,
            check=False,
        )

    def test_the_three_session_hooks_produce_one_run(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        for payload in (SESSION_START, PROMPT_SUBMIT, SESSION_END):
            completed = self._deliver(payload, data)
            assert completed.returncode == 0, completed.stderr

        with AER(data) as runtime:
            runs = runtime.list_runs()
            assert len(runs) == 1
            run = runs[0]
            assert run.task_description == task_summary(str(load_fixture(PROMPT_SUBMIT)["prompt"]))
            assert run.status is RunStatus.INCONCLUSIVE
            assert run.is_finished is True
            assert run.agent_name == "codex"
            assert run.metadata["provider"] == CODEX_PROVIDER
            assert run.metadata["adapter"]["name"] == ADAPTER_NAME
            assert [event.event_type.value for event in runtime.get_events(run.id)] == [
                "TASK_START",
                "TASK_END",
            ]

            session = runtime.find_adapter_session(
                CODEX_PROVIDER, str(load_fixture(PROMPT_SUBMIT)["session_id"])
            )
            assert session is not None and session.aer_run_id == run.id

    def test_the_shipped_coverage_can_be_printed(self, tmp_path: Path) -> None:
        """Section 54's cheap operator surface: a Python call, not a dashboard."""
        completed = self._deliver(SESSION_START, tmp_path / "data", "--print-coverage")

        assert completed.returncode == 0
        assert VERIFIED_CODEX_VERSION in completed.stdout
        # Three levels, not a tick: a contract-only mapping and a captured payload are
        # different claims (section 19). No contract fixtures remain, so the middle level
        # is absent -- which is what the assertion below pins.
        assert "CAPTURED" in completed.stdout
        assert "MISSING" in completed.stdout
        assert "CONTRACT_ONLY" not in completed.stdout
        # Both modes are reported, because a claim about one says nothing about the other.
        assert "exec mode" in completed.stdout
        assert "interactive mode" in completed.stdout

    def test_a_delivery_that_is_not_a_codex_payload_fails_loudly(self, tmp_path: Path) -> None:
        env = {**os.environ, "AER_DATA_DIR": str(tmp_path / "data")}
        completed = subprocess.run(
            [sys.executable, "-m", "aer.adapter.codex.hook"],
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

    def test_a_failed_delivery_does_not_block_and_writes_nothing(self, tmp_path: Path) -> None:
        """A broken AER must never take the agent down (section 8)."""
        data = tmp_path / "data"
        completed = self._deliver(SESSION_START, data, "--print-coverage")
        assert completed.returncode == 0
        # No run was created by printing the coverage.
        with AER(data) as runtime:
            assert runtime.list_runs() == []


class TestMappingRejections:
    def test_an_unknown_hook_event_is_refused(self) -> None:
        with pytest.raises(UnsupportedAdapterEvent, match="does not know"):
            event_name({"hook_event_name": "SomethingNew", "session_id": "s"})

    def test_a_payload_without_an_event_name_is_refused(self) -> None:
        with pytest.raises(AdapterProtocolError, match="hook_event_name"):
            event_name({"session_id": "s"})

    def test_a_payload_without_a_session_is_refused(self) -> None:
        with pytest.raises(AdapterProtocolError, match="session_id"):
            session_id({"hook_event_name": "SessionStart"})

    def test_an_unknown_field_does_not_break_the_mapping(self, codex_adapter: CodexAdapter) -> None:
        """Section 46: unknown fields are tolerated; unknown *shapes* are not."""
        payload = {**load_fixture(SESSION_START), "something_new": {"nested": True}}

        assert codex_adapter.handle_event(payload) == ()


class TestToolMapping:
    """Written against captured payloads, not against a hand-written shape.

    Two things the real payloads settled, both of which the earlier contract fixtures had
    got wrong or left open:

    * ``tool_use_id`` is present on ``PreToolUse`` **and** ``PostToolUse``, and a captured
      run showed a 1:1 match, so a call and its result can be correlated (section 8);
    * ``tool_response`` is a **plain string**. There is no ``exit_code``, no ``success``
      and no ``error`` anywhere in the payload, so a tool that failed is distinguishable
      only by the words in its output -- which AER does not read (section 52, D-097).
    """

    def test_a_tool_call_becomes_a_tool_call(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))

        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_CALL))

        events = codex_runtime.get_events(handle.run_id)
        assert [event.event_type.value for event in events] == ["TASK_START", "TOOL_CALL"]
        assert events[-1].input["tool"] == "Bash"
        assert events[-1].input["tool_use_id"]
        assert events[-1].metadata["source"] == f"adapter:{ADAPTER_NAME}"

    def test_the_tool_input_is_summarised_not_stored_whole(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Section 13: a summary of the call, never the whole payload."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))

        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_CALL))

        event = codex_runtime.get_events(handle.run_id)[-1]
        assert event.input["tool"] == "Bash"
        assert event.metadata["codex"]["event"] == "PreToolUse"

    def test_a_successful_tool_result_states_no_outcome(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """The payload never says the tool worked, so neither does AER.

        The tri-state survives contact with reality: ``success`` is absent rather than
        ``true``, because claiming success here would be a claim Codex never made.
        """
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))

        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_SUCCESS))

        result = codex_runtime.get_events(handle.run_id)[-1]
        assert result.event_type is EventType.TOOL_RESULT
        assert "success" not in result.output
        assert "On branch master" in result.output["result"]
        assert codex_runtime.get_errors(handle.run_id) == []

    def test_a_failing_tool_result_writes_no_error(self, tmp_path: Path) -> None:
        """Section 15 meets the real payload, and the real payload wins.

        The captured failure carries a Python traceback as *text* inside
        ``tool_response``. Turning that into an ``ERROR`` would mean reading English out
        of output, which section 52 forbids and which is unreliable anyway -- a grep for
        "passed" in a failed run's output is the obvious counterexample. So the honest
        record is a TOOL_RESULT whose outcome is unstated, and no error row.
        """
        with AER(tmp_path / "data") as runtime:
            adapter = CodexAdapter(agent_version="0.155.1", detect_version=False)
            handle = runtime.open_adapter_session(adapter, load_fixture(PROMPT_SUBMIT))

            runtime.ingest_adapter_event(handle, load_fixture(TOOL_FAILURE))

            types = [event.event_type.value for event in runtime.get_events(handle.run_id)]
            assert types == ["TASK_START", "TOOL_RESULT"]
            assert runtime.get_errors(handle.run_id) == []
            result = runtime.get_events(handle.run_id)[-1]
            assert "success" not in result.output
            assert "AssertionError" in result.output["result"]

    def test_the_call_and_its_result_share_a_correlation_id(self) -> None:
        """Section 8: linkage is available, not unavailable."""
        call = load_fixture(TOOL_CALL)
        result = load_fixture(TOOL_SUCCESS)

        assert call["tool_use_id"] == result["tool_use_id"]

    def test_the_tool_input_shape_holds_for_a_second_tool(self) -> None:
        """``tool_input`` is tool-specific in *content* and uniform in *shape*.

        Captured: for ``apply_patch`` it is the patch text under the same ``command`` key
        that ``Bash`` uses for a shell command. The adapter summarises the whole object
        rather than reaching for a field, so a third tool with a third shape needs no
        change here -- and the summary is what keeps a patch from being stored verbatim.
        """
        payload = load_fixture(TOOL_PATCH)

        assert payload["tool_name"] == "apply_patch"
        assert isinstance(payload["tool_input"], dict)
        assert payload["tool_input"]["command"].startswith("*** Begin Patch")
        assert payload["tool_input"] != load_fixture(TOOL_CALL)["tool_input"]

    def test_a_patch_result_is_recorded_as_a_result(self, tmp_path: Path) -> None:
        with AER(tmp_path / "data") as runtime:
            adapter = CodexAdapter(agent_version="0.155.1", detect_version=False)
            handle = runtime.open_adapter_session(adapter, load_fixture(PROMPT_SUBMIT))

            runtime.ingest_adapter_event(handle, load_fixture(TOOL_PATCH_RESULT))

            result = runtime.get_events(handle.run_id)[-1]
            assert result.event_type is EventType.TOOL_RESULT
            # "Exit code: 0" sits inside the text. Reading it would be parsing prose, and
            # it is deliberately not read (see the fixture manifest).
            assert "Exit code" in result.output["result"]
            assert "success" not in result.output


class TestRefusals:
    """The four claims an integration is most tempted to invent."""

    def test_a_failed_tool_then_a_good_one_is_not_a_recovery(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Section 21: no explicit repair linkage, so no recovery is manufactured."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))

        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_FAILURE))
        success = {**load_fixture(TOOL_SUCCESS), "tool_name": "editor", "tool_use_id": "other"}
        codex_runtime.ingest_adapter_event(handle, success)

        types = [event.event_type.value for event in codex_runtime.get_events(handle.run_id)]
        assert "RECOVERY_START" not in types
        assert "RECOVERY_RESULT" not in types
        assert codex_runtime.get_recoveries(handle.run_id) == []

    def test_codex_saying_it_worked_creates_no_verdict(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Sections 22-23: a statement is not a verification, whatever it says."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_SUCCESS))

        codex_runtime.close_adapter_session(handle, load_fixture(SESSION_END))

        assert codex_runtime.get_verifications(handle.run_id) == []
        assert codex_runtime.verified_success(handle.run_id) is False

    def test_the_adapter_cannot_offer_verification_evidence(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """It does not declare the capability, so the ingestor refuses (section 39)."""
        from aer import HttpStatusVerifier

        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))

        with pytest.raises(AdapterCapabilityError, match="external_verification"):
            codex_runtime.adapter_ingestor.verify(
                handle,
                HttpStatusVerifier(expected_status=200),
                payload={"actual_status": 200},
            )

    def test_a_tool_succeeding_is_not_an_experience_being_adopted(
        self, codex_adapter: CodexAdapter, codex_runtime: AER, index
    ) -> None:
        """Sections 5 and 27: the strongest false claim available to a tool hook."""
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(codex_runtime)
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        tracked = retrieve(codex_runtime, index, guidance=[hit], run_id=handle.run_id)

        with pytest.raises(AdapterCapabilityError, match="explicit_adoption_signal"):
            codex_runtime.adapter_ingestor.record_usage_signal(
                handle, tracked, experience_id="exp-1", signal=UsageSignal.ADOPTED
            )

        stored = codex_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.usage_signal is UsageSignal.UNKNOWN
        assert stored.utility_label is UtilityLabel.UNKNOWN

    def test_the_adapter_cannot_claim_an_experience_helped(
        self, codex_adapter: CodexAdapter, codex_runtime: AER, index
    ) -> None:
        """Section 34: a successful run is not a utility signal."""
        from tests.usage.support import indexed_for, retrieve

        hit = indexed_for(codex_runtime)
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        tracked = retrieve(codex_runtime, index, guidance=[hit], run_id=handle.run_id)

        with pytest.raises(AdapterCapabilityError, match="explicit_utility_signal"):
            codex_runtime.adapter_ingestor.record_utility(
                handle, tracked, experience_id="exp-1", label=UtilityLabel.HELPFUL
            )

    def test_the_hook_never_records_an_injection(self, tmp_path: Path) -> None:
        """Section 53, asserted end to end: a hook running is not an injection.

        Codex 0.155.1 offers no hook output that puts text into the model's context on
        this adapter's behalf, so the number of recorded injections after a full session
        of deliveries must be zero. If a future Codex adds the capability, this test
        fails and the claim gets revisited rather than quietly becoming false.
        """
        data = tmp_path / "data"
        for payload in (SESSION_START, PROMPT_SUBMIT, TOOL_CALL, TOOL_FAILURE, SESSION_END):
            completed = TestTheHookEntryPoint._deliver(payload, data)
            assert completed.returncode == 0, completed.stderr

        with AER(data) as runtime:
            assert runtime.experience_usage.count() == 0
            assert runtime.retrieval_sessions.count() == 0


class TestIdempotency:
    def test_the_same_tool_result_twice_is_one_aer_event(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Section 47: Codex hooks have been seen to fire more than once."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        payload = load_fixture(TOOL_SUCCESS)

        first = codex_runtime.ingest_adapter_event(handle, payload)
        second = codex_runtime.ingest_adapter_event(handle, payload)

        assert first.outcome.value == "APPLIED"
        assert second.outcome.value == "DUPLICATE"
        types = [event.event_type.value for event in codex_runtime.get_events(handle.run_id)]
        assert types.count("TOOL_RESULT") == 1

    def test_a_repeated_session_end_finishes_once(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """The three session hooks are handled outside the ledger, so they must be
        idempotent by construction -- and this is where that is checked."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        payload = load_fixture(SESSION_END)

        first = codex_runtime.close_adapter_session(handle, payload)
        second = codex_runtime.close_adapter_session(handle, payload)

        assert first is not None and second is not None
        assert second.status is first.status
        assert [event.event_type.value for event in codex_runtime.get_events(handle.run_id)].count(
            "TASK_END"
        ) == 1

    def test_repeated_prompts_resume_rather_than_duplicate(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Section 9: the same Codex session maps to one AER run."""
        payload = load_fixture(PROMPT_SUBMIT)

        first = codex_runtime.open_adapter_session(codex_adapter, payload)
        second = codex_runtime.open_adapter_session(codex_adapter, payload)

        assert first.run_id == second.run_id
        assert second.outcome.value == "RESUMED"
        assert len(codex_runtime.list_runs()) == 1

    def test_the_fingerprint_is_stable_and_discriminating(self) -> None:
        """Section 29: same delivery collides, different deliveries must not."""
        prompt = load_fixture(PROMPT_SUBMIT)
        other_turn = {**prompt, "turn_id": "a-different-turn"}
        other_session = {**prompt, "session_id": "a-different-session"}

        event = CodexHookEvent.USER_PROMPT_SUBMIT
        assert external_event_id(event, prompt) == external_event_id(event, dict(prompt))
        assert external_event_id(event, prompt) != external_event_id(event, other_turn)
        assert external_event_id(event, prompt) != external_event_id(event, other_session)

    def test_a_different_prompt_in_the_same_turn_is_a_different_event(self) -> None:
        prompt = load_fixture(PROMPT_SUBMIT)
        edited = {**prompt, "prompt": "say hello instead"}

        assert external_event_id(CodexHookEvent.USER_PROMPT_SUBMIT, prompt) != external_event_id(
            CodexHookEvent.USER_PROMPT_SUBMIT, edited
        )


class TestSanitization:
    def test_a_secret_in_a_prompt_does_not_reach_the_task(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        payload = {
            **load_fixture(PROMPT_SUBMIT),
            "prompt": "use api_key=sk-ABCDEFGHIJKLMNOPQRSTUV to call the API",
        }

        handle = codex_runtime.open_adapter_session(codex_adapter, payload)

        run = codex_runtime.get_run(handle.run_id)
        assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in run.task_description

    def test_a_huge_prompt_becomes_a_short_task(self) -> None:
        assert len(task_summary("x" * 5000)) <= 200

    def test_a_multi_line_prompt_becomes_one_line(self) -> None:
        assert task_summary("first line\n\nsecond line") == "first line"


class TestCoverage:
    def test_the_shipped_matrix_refuses_to_claim_the_unobserved(self) -> None:
        """Section 37-38: the gaps are the deliverable, not a footnote."""
        coverage = default_coverage()

        # Captured from a real session.
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            assert coverage.observed(CodexMode.EXEC, event) is True
        # Never observed: nothing in the probed session shape produced them.
        assert coverage.observed(CodexMode.EXEC, "PermissionRequest") is False
        assert coverage.observed(CodexMode.EXEC, "Interrupt") is False
        # A different mode is a different claim (section 36).
        assert coverage.observed(CodexMode.INTERACTIVE, "SessionStart") is False
        assert "SessionStart" in coverage.missing(CodexMode.INTERACTIVE)
        # And no cell is allowed to sit at the middle level any more.
        assert coverage.contract_only(CodexMode.EXEC) == ()

    def test_a_complete_exec_session_is_recognised(self) -> None:
        coverage = default_coverage()

        assert (
            coverage.completeness(
                CodexMode.EXEC, fired=("SessionStart", "UserPromptSubmit", "SessionEnd")
            )
            is SessionCompleteness.FULL
        )
        assert (
            coverage.completeness(CodexMode.EXEC, fired=("UserPromptSubmit",))
            is SessionCompleteness.PARTIAL
        )
        assert coverage.completeness(CodexMode.EXEC, fired=()) is SessionCompleteness.UNKNOWN

    def test_the_identities_capabilities_are_the_measured_ones(
        self, codex_adapter: CodexAdapter
    ) -> None:
        capabilities = codex_adapter.capabilities()

        assert capabilities.tool_events is True
        assert capabilities.session_linkage is True
        assert capabilities.explicit_adoption_signal is False
        assert capabilities.explicit_utility_signal is False
        assert capabilities.external_verification is False

    def test_the_adapter_says_which_versions_it_was_tested_against(
        self, codex_adapter: CodexAdapter
    ) -> None:
        """Section 58: one measured version, never "all future versions"."""
        assert codex_adapter.tested_codex_versions == (VERIFIED_CODEX_VERSION,)


class TestHostIndependence:
    def test_the_mapping_never_needs_a_real_cli(self) -> None:
        """Every mapping decision is a pure function of the payload."""
        assert task_summary("do the thing") == "do the thing"
        assert finish_status({"reason": "complete"}) is RunStatus.SUCCESS
        assert event_name({"hook_event_name": "Stop", "session_id": "s"}) is CodexHookEvent.STOP

    def test_version_detection_is_honest_when_the_cli_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing CLI is reported, never guessed at.

        ``subprocess.run`` is patched rather than ``PATH``: emptying the environment makes
        the OS return bytes the reader thread cannot decode, which surfaces as an
        unrelated warning and hides what is being tested.
        """
        import subprocess as subprocess_module

        def explode(*args: object, **kwargs: object) -> object:
            raise FileNotFoundError("codex is not installed")

        monkeypatch.setattr(subprocess_module, "run", explode)

        assert detect_codex_version() == "unknown"

    def test_the_adapter_reports_unknown_rather_than_guessing_a_version(self) -> None:
        assert CodexAdapter(agent_version=None)._agent_version in {
            "unknown",
            detect_codex_version(),
        }


class TestGenericAdapterIsUntouched:
    def test_codex_specifics_did_not_leak_into_the_generic_adapter(self) -> None:
        """The protocol is still the boundary: no Codex vocabulary on the generic side."""
        generic = GenericAgentAdapter()

        assert "codex" not in generic.name
        assert generic.protocol_version == "1"

    def test_the_hook_module_is_the_only_place_that_runs_a_command(self) -> None:
        """Sanity guard: the mapping layer stays pure, so it stays testable."""
        import aer.adapter.codex.mapping as mapping_module

        source = Path(mapping_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "open(", "AER("):
            assert forbidden not in source


class TestCrashGap:
    """Section 48: what a process killed between claim and apply leaves behind.

    M8's ledger claims an external event before applying it, which is what makes duplicate
    delivery harmless. The cost is the other direction: a crash in between loses that one
    event, and the retry is then treated as a duplicate. That trade is deliberate, and
    this class is where its two promises are checked -- it is *detectable*, and it is
    *safe*.
    """

    def test_an_unapplied_claim_is_visible(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        payload = load_fixture(TOOL_SUCCESS)

        # Claim without applying: exactly the state a killed process leaves.
        claimed = codex_runtime.adapter_ingestor._claim(
            handle,
            codex_adapter.handle_event(payload),
            mapping.external_event_id(mapping.CodexHookEvent.POST_TOOL_USE, payload),
        )
        assert claimed[1] is not None and claimed[1].is_applied is False

        assert codex_runtime.adapter_events.count(applied=False) == 1
        assert codex_runtime.adapter_status()["unapplied_events"] == 1
        outstanding = codex_runtime.adapter_events.list(applied=False)
        assert [entry.external_event_id for entry in outstanding] == [
            mapping.external_event_id(mapping.CodexHookEvent.POST_TOOL_USE, payload)
        ]

    def test_a_retry_after_a_crash_does_not_double_apply(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """At-most-once, stated as a test rather than as a hope."""
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        payload = load_fixture(TOOL_SUCCESS)
        codex_runtime.adapter_ingestor._claim(
            handle,
            codex_adapter.handle_event(payload),
            mapping.external_event_id(mapping.CodexHookEvent.POST_TOOL_USE, payload),
        )

        retry = codex_runtime.ingest_adapter_event(handle, payload)

        assert retry.outcome.value == "DUPLICATE"
        types = [event.event_type.value for event in codex_runtime.get_events(handle.run_id)]
        assert types.count("TOOL_RESULT") == 0

    def test_the_recovery_path_is_a_query_and_not_a_replay(self) -> None:
        """The operator path is ``adapter_status`` / ``adapter_events.list(applied=False)``.

        A replay would need the normalized envelope to have been persisted; the M8 ledger
        stores identity and provenance rather than content, so there is nothing to replay
        *with*. The honest consequence is that a crash loses one event and says so, and
        this test records that no replay is claimed.
        """
        import aer.adapter.codex as codex_package

        source = Path(codex_package.__file__).read_text(encoding="utf-8")
        assert "replay" not in source.lower().replace("replaying", "noop")

        # What does exist: a query the operator can run.
        assert hasattr(AER, "adapter_status")


class TestAmbiguousTerminationIsNotAFailure:
    """Section 13: a session closing is not a task failing.

    The risk this class exists for is not a missing feature, it is a *plausible wrong
    row*. A Codex session that ends without stating an outcome leaves a run whose status
    is ABORTED, and ABORTED has always meant "the agent declared the task abandoned" --
    which drives the distillation policy's FAILED_RUN trigger and produces a ``FAILURE``
    experience. For an in-process agent that reading is right. For an integration that
    closed the run because the *session* ended, it is a fabricated failure.
    """

    def test_the_run_records_that_nobody_declared_an_outcome(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        handle = codex_runtime.open_adapter_session(codex_adapter, load_fixture(PROMPT_SUBMIT))
        codex_runtime.ingest_adapter_event(handle, load_fixture(TOOL_SUCCESS))

        codex_runtime.close_adapter_session(handle, load_fixture(SESSION_END))

        run = codex_runtime.get_run(handle.run_id)
        assert run is not None
        assert run.status is RunStatus.INCONCLUSIVE
        # And it is the *status* that carries the fact, not a metadata flag beside it:
        # one representation, so the policy and the classifier cannot disagree.
        assert "outcome_declared" not in run.metadata

    def test_no_failure_experience_is_produced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end assertion: no experience at all, and never a FAILURE one."""
        data = tmp_path / "data"
        for payload in (SESSION_START, PROMPT_SUBMIT, TOOL_SUCCESS, SESSION_END):
            completed = TestTheHookEntryPoint._deliver(payload, data)
            assert completed.returncode == 0, completed.stderr

        with AER(data) as runtime:
            runs = runtime.list_runs()
            assert len(runs) == 1
            run_id = runs[0].id

            decision = runtime.evaluate_distillation(run_id)
            assert decision.should_distill is False
            assert "no required verification decided one" in " ".join(decision.reasons)
            assert decision.kind is None

            # And the pipeline agrees: nothing is learned from a run nobody described.
            assert runtime.experience_service.is_configured is False

    def test_the_policy_still_learns_from_a_declared_failure(self, tmp_path: Path) -> None:
        """The protection is narrow: a *declared* failure is still evidence."""
        with AER(tmp_path / "data") as runtime:
            context = runtime.start_run(task="a task the agent gave up on")
            context.fail()
            run_id = context.run_id

            decision = runtime.evaluate_distillation(run_id)

            assert decision.should_distill is True
            assert decision.kind is not None
            assert decision.kind.value == "FAILURE"

    def test_a_declared_failure_with_an_error_is_still_distilled(self, tmp_path: Path) -> None:
        """Other triggers keep working for an undeclared ending too: a recorded error is
        evidence in its own right, whatever the ending said."""
        with AER(tmp_path / "data") as runtime:
            context = runtime.start_run(task="a task with a recorded error")
            try:
                with context.tool("shell"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            context.abort()
            run_id = context.run_id

            decision = runtime.evaluate_distillation(run_id)

            assert decision.should_distill is True


class TestAnInconclusiveCodexRunCanStillBeEstablished:
    """Round-8.1.1: the closed loop for an agent that cannot report an outcome.

    A Codex session always ends without a declared result (``reason: "other"``), so under
    the previous mapping every Codex run was a failure candidate and none of them could
    ever be a verified success. That was not a limitation of Codex -- it was AER having
    no way to say "the run is over and nobody said how it went".

    This class walks the loop with the captured payloads, through the real ingestor:

        Codex session -> INCONCLUSIVE -> independent verification -> verified success
                      -> a SUCCESS experience, and never a failure one
    """

    @staticmethod
    def _run_a_session(runtime: AER, adapter: CodexAdapter) -> str:
        """Drive the three session hooks and the tool result through the ingestor.

        In process rather than through the hook command: ``TestTheHookEntryPoint``
        already proves the command works on the same fixtures, and re-proving it here
        would mean twenty subprocess launches to test something else.
        """
        handle = runtime.open_adapter_session(adapter, load_fixture(PROMPT_SUBMIT))
        runtime.ingest_adapter_event(handle, load_fixture(TOOL_SUCCESS))
        runtime.close_adapter_session(handle, load_fixture(SESSION_END))
        return handle.run_id

    @staticmethod
    def _verify(runtime: AER, run_id: str, *, actual_status: int) -> None:
        """An independent check, recorded through the runtime's own post-hoc entry point.

        Not through the adapter: it has no ``external_verification`` capability, and an
        adapter that could write a verdict would be the assessor sitting on the panel.
        """
        runtime.verify(
            run_id,
            HttpStatusVerifier(expected_status=200, required=True),
            context=VerificationContext(run_id=run_id, payload={"actual_status": actual_status}),
        )

    def test_the_session_ends_inconclusive_and_unverified(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Step one: the run is over, and AER knows nothing about how it went."""
        run_id = self._run_a_session(codex_runtime, codex_adapter)

        assert codex_runtime.get_run(run_id).status is RunStatus.INCONCLUSIVE
        assert codex_runtime.verified_success(run_id) is False
        assert codex_runtime.get_verifications(run_id) == []

    def test_a_required_check_establishes_the_outcome(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Step two: the missing declaration is replaced by evidence, not by a guess."""
        run_id = self._run_a_session(codex_runtime, codex_adapter)

        self._verify(codex_runtime, run_id, actual_status=200)

        # The declaration is never rewritten: verification is a separate recorded fact.
        assert codex_runtime.get_run(run_id).status is RunStatus.INCONCLUSIVE
        assert codex_runtime.verified_success(run_id) is True

    def test_the_verified_run_distils_as_a_success_and_never_as_a_failure(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Step three: what the milestone was for.

        Before this, a Codex run could not be classified as anything but a failure, and
        the weaker version of that bug -- "a tool result a human would read as a failure
        makes it one" -- is still the failure mode this asserts against. The assertion is
        deliberately written as a rejection of the alternative: a plausible-looking wrong
        kind is worse than no kind.
        """
        run_id = self._run_a_session(codex_runtime, codex_adapter)
        self._verify(codex_runtime, run_id, actual_status=200)

        # Marked high value because this session is *uneventful* -- one successful tool
        # call, a passing check, nothing odd -- and an uneventful verified success teaches
        # nothing. That is the policy's deliberate bias and it applies to a Codex run
        # exactly as it does to a declared one; ``explicit_high_value`` is the documented
        # way to keep such a run, and it is used here to expose the *kind*.
        decision = codex_runtime.evaluate_distillation(run_id, explicit_high_value=True)

        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.SUCCESS
        assert decision.kind is not ExperienceKind.FAILURE

    def test_without_the_check_there_is_still_no_experience(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """And the other half: evidence is what earns the experience, not the hooks.

        Nothing here says the task failed. It says AER was never told and never found
        out -- a state the store should record as an absence, not as a lesson.
        """
        run_id = self._run_a_session(codex_runtime, codex_adapter)

        decision = codex_runtime.evaluate_distillation(run_id)

        assert decision.should_distill is False
        assert decision.kind is None

    def test_a_failed_required_check_still_proves_a_failure(
        self, codex_adapter: CodexAdapter, codex_runtime: AER
    ) -> None:
        """Being unable to declare is not being unable to be judged.

        When an independent check says the goal was not met, that is a proven failure and
        it outranks the missing declaration -- the same precedence a declared success
        gets (Milestone 4, section 26).
        """
        run_id = self._run_a_session(codex_runtime, codex_adapter)

        self._verify(codex_runtime, run_id, actual_status=403)

        decision = codex_runtime.evaluate_distillation(run_id)

        assert codex_runtime.verified_success(run_id) is False
        assert decision.should_distill is True
        assert decision.kind is ExperienceKind.FAILURE
