"""Milestone 8 acceptance: the same evidence from two unrelated Agents.

Two scenarios, and the second is the one this milestone is judged on.

**The core loop** (section 33): a run retrieves a verified experience, injects it,
records an explicit adoption, acts, and passes verification -- all of it driven by an
adapter, with the usage numbers landing in M7 exactly as an in-process caller's would.

**Two vocabularies, one result** (section 60): a shell-style fake agent and a
structured-tool-API fake agent, whose payloads share nothing but the protocol, produce
runs whose AER semantics are identical. That is the claim that makes Codex, Claude Code,
Cursor and DSH worth building adapters for, and it is checkable today, without any of
them in the loop.

Section 34 is checked in both: the adapter never writes ``HELPFUL``, no matter how the
task ended.
"""

from __future__ import annotations

from aer import (
    AER,
    EventType,
    GenericAgentAdapter,
    HttpStatusVerifier,
    UsageSignal,
    UtilityLabel,
)
from aer.adapter import AdapterIngestor
from aer.knowledge.formatter import ExperienceContextFormatter
from aer.usage.fingerprints import context_fingerprint
from tests.adapters.support import (
    DEFAULT_QUERY,
    ShellAgent,
    ShellStyleAdapter,
    StructuredAgent,
    StructuredStyleAdapter,
)
from tests.usage.support import indexed_for, script_retrieval, store_experience


class TestTheCoreScenario:
    def test_an_adapter_drives_the_whole_loop_end_to_end(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Section 33: retrieve, inject, adopt, act, verify -- through the protocol."""
        # A verified recovery experience from a previous task.
        hit = indexed_for(adapter_runtime, experience_id="exp-403")
        script_retrieval(index, guidance=[hit])

        agent = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="s-403")
        handle = agent.connect()
        assert handle.outcome.value == "STARTED"

        # 1) retrieve for this run
        tracked = ingestor.retrieve(handle, DEFAULT_QUERY, domain="wordpress")
        assert [item.experience_id for item in tracked.result.all_hits] == ["exp-403"]

        # 2) the adapter records what really entered the context
        rows = ingestor.inject(handle, tracked)
        assert len(rows) == 1
        assert rows[0].is_injected is True
        assert rows[0].injection_chars and rows[0].injection_chars > 0
        assert rows[0].context_fingerprint == context_fingerprint(
            ExperienceContextFormatter().format(tracked.result)
        )

        # 3) the platform can see that the agent picked the experience up
        ingestor.record_usage_signal(
            handle, tracked, experience_id="exp-403", signal=UsageSignal.ADOPTED
        )

        # 4) the work
        agent.call("http.request", parameters={"url": "https://example.test/wp"})
        agent.outcome("http.request", ok=True, payload={"status": 200})

        # 5) it ended well, and an independent check agrees
        agent.finish(outcome="success")
        ingestor.verify(
            handle,
            HttpStatusVerifier(expected_status=200, required=True),
            payload={"actual_status": 200},
        )

        report = adapter_runtime.experience_effectiveness("exp-403")
        assert report.retrieval_count == 1
        assert report.injection_count == 1
        assert report.explicit_adoption_count == 1
        assert report.verified_success_runs == 1
        assert report.observed_success_rate == 1.0
        assert report.adopted_verified_success_rate == 1.0

    def test_the_adapter_never_writes_helpful(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Section 34: success is not a utility signal, and the adapter may not pretend."""
        hit = indexed_for(adapter_runtime, experience_id="exp-403")
        script_retrieval(index, guidance=[hit])
        agent = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="s-403")
        handle = agent.connect()
        tracked = ingestor.retrieve(handle, DEFAULT_QUERY, domain="wordpress")
        ingestor.inject(handle, tracked)
        ingestor.record_usage_signal(
            handle, tracked, experience_id="exp-403", signal=UsageSignal.ADOPTED
        )
        agent.finish(outcome="success")
        ingestor.verify(
            handle,
            HttpStatusVerifier(expected_status=200, required=True),
            payload={"actual_status": 200},
        )

        report = adapter_runtime.experience_effectiveness("exp-403")

        assert report.verified_success_runs == 1
        assert report.helpful_count == 0
        assert report.harmful_count == 0
        stored = adapter_runtime.get_experience_usage(tracked.session_id, "exp-403")
        assert stored is not None
        assert stored.utility_label is UtilityLabel.UNKNOWN

    def test_a_failure_is_still_recorded_as_evidence(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """The negative case matters as much: an experience used into a failure."""
        hit = indexed_for(adapter_runtime, experience_id="exp-403")
        script_retrieval(index, guidance=[hit])
        agent = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="s-403")
        handle = agent.connect()
        tracked = ingestor.retrieve(handle, DEFAULT_QUERY, domain="wordpress")
        ingestor.inject(handle, tracked)
        ingestor.record_usage_signal(
            handle, tracked, experience_id="exp-403", signal=UsageSignal.ADOPTED
        )
        agent.call("http.request")
        agent.outcome("http.request", ok=False)
        agent.finish(outcome="success")
        ingestor.verify(
            handle,
            HttpStatusVerifier(expected_status=200, required=True),
            payload={"actual_status": 403},
        )

        report = adapter_runtime.experience_effectiveness("exp-403")

        assert report.verified_failure_runs == 1
        assert report.verified_success_runs == 0
        assert report.adopted_verified_success_rate == 0.0
        assert report.harmful_count == 0

    def test_an_unattributed_retrieval_is_reported_rather_than_guessed(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Retrieving without a run is allowed, and M7 says so honestly."""
        hit = indexed_for(adapter_runtime, experience_id="exp-403")
        script_retrieval(index, guidance=[hit])

        tracked = adapter_runtime.retrieve_for_run(DEFAULT_QUERY, run_id=None)

        report = adapter_runtime.experience_effectiveness("exp-403")
        assert tracked.session_id
        assert report.retrieval_count == 1
        assert report.unattributed_usage_count == 1
        assert report.observed_success_rate is None


class TestTwoAgentsOneProtocol:
    """Section 60: the acceptance criterion for the whole milestone."""

    @staticmethod
    def _run_shell(ingestor: AdapterIngestor, runtime: AER) -> str:
        agent = ShellAgent(ingestor, ShellStyleAdapter(), session_id="shell-run")
        handle = agent.connect()
        agent.run("check-permissions --page 123", exit_code=1, stderr="403 Forbidden")
        agent.retry("grant edit_posts and retry")
        agent.run("update-page --page 123", exit_code=0, stdout="200 OK")
        agent.retry_done(ok=True)
        agent.finish(ok=True)
        return handle.run_id

    @staticmethod
    def _run_structured(ingestor: AdapterIngestor, runtime: AER) -> str:
        agent = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="struct-run")
        handle = agent.connect()
        agent.call("http.request", parameters={"url": "/wp-json/wp/v2/pages/123"})
        agent.outcome("http.request", ok=False, code="http.403")
        agent.repair("grant edit_posts and retry")
        agent.call("http.request", parameters={"url": "/wp-json/wp/v2/pages/123"})
        agent.outcome("http.request", ok=True, payload={"status": 200})
        agent.repair_done(repaired=True)
        agent.finish(outcome="success")
        return handle.run_id

    def test_both_agents_produce_the_same_event_types(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        shell_run = self._run_shell(ingestor, adapter_runtime)
        structured_run = self._run_structured(ingestor, adapter_runtime)

        shell_types = [event.event_type.value for event in adapter_runtime.get_events(shell_run)]
        structured_types = [
            event.event_type.value for event in adapter_runtime.get_events(structured_run)
        ]

        assert shell_types == structured_types
        # CALL -> ERROR -> RESULT, which is the same order the in-process tool hook
        # writes: the failure is recorded before the result that reports it.
        assert shell_types == [
            "TASK_START",
            "TOOL_CALL",
            "ERROR",
            "TOOL_RESULT",
            "RECOVERY_START",
            "TOOL_CALL",
            "TOOL_RESULT",
            "RECOVERY_RESULT",
            "TASK_END",
        ]

    def test_both_agents_produce_the_same_semantics_from_different_words(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        shell_run = self._run_shell(ingestor, adapter_runtime)
        structured_run = self._run_structured(ingestor, adapter_runtime)

        for run_id in (shell_run, structured_run):
            events = adapter_runtime.get_events(run_id)
            errors = adapter_runtime.get_errors(run_id)
            recoveries = adapter_runtime.get_recoveries(run_id)

            assert len(errors) == 1
            assert len(recoveries) == 1
            assert recoveries[0].success is True
            # Resolution stays explicit: neither fake knows AER's error id, and a
            # successful repair does not retroactively resolve a failure it was never
            # linked to (round-3 brief, section 18). The link, when a platform can
            # supply one, is covered in test_ingest.
            assert errors[0].resolved is False
            assert adapter_runtime.get_run(run_id).status.value == "SUCCESS"
            # A failure, then a successful repair, then success: a recovery trajectory.
            results = [event for event in events if event.event_type is EventType.TOOL_RESULT]
            assert [event.output["success"] for event in results] == [False, True]

    def test_the_runs_are_distinguishable_by_their_recorded_identity(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 38: comparing integrations later needs to know which produced what."""
        shell_run = self._run_shell(ingestor, adapter_runtime)
        structured_run = self._run_structured(ingestor, adapter_runtime)

        shell = adapter_runtime.get_run(shell_run)
        structured = adapter_runtime.get_run(structured_run)

        assert shell.metadata["provider"] == "shell-agent"
        assert shell.metadata["adapter"]["name"] == "aer-shell"
        assert structured.metadata["provider"] == "structured-agent"
        assert structured.metadata["adapter"]["name"] == "aer-structured"
        assert shell.metadata["external_session_id"] == "shell-run"
        assert structured.metadata["external_session_id"] == "struct-run"

    def test_the_provenance_of_every_event_names_its_adapter(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        shell_run = self._run_shell(ingestor, adapter_runtime)
        structured_run = self._run_structured(ingestor, adapter_runtime)

        for run_id, adapter_name in (
            (shell_run, "aer-shell"),
            (structured_run, "aer-structured"),
        ):
            for event in adapter_runtime.get_events(run_id):
                if event.event_type in (EventType.TASK_START, EventType.TASK_END):
                    # The lifecycle events belong to the runtime, and the adapter's
                    # identity is recorded on the run rather than on them (section 38).
                    assert "source" not in event.metadata
                    continue
                assert event.metadata["source"] == f"adapter:{adapter_name}"

    def test_a_third_agent_needs_no_core_change(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """The reference adapter alone is enough to drive the whole pipeline.

        Section 60's claim is about the *core* not changing, so the same scenario is run
        through the generic adapter as well -- three vocabularies, one runtime, no
        special cases anywhere in `aer/`.
        """
        agent = GenericAgentAdapter()
        handle = ingestor.open(agent, {"session_id": "generic-run", "task": "fix the 403"})
        ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})
        ingestor.ingest(
            handle,
            {
                "type": "tool_result",
                "tool": "shell",
                "success": False,
                "error": {"error_type": "shell.NonZeroExit", "message": "403"},
                "event_id": "e2",
            },
        )
        ingestor.ingest(
            handle, {"type": "recovery_start", "reason": "retry with rights", "event_id": "e3"}
        )
        ingestor.ingest(handle, {"type": "recovery_result", "success": True, "event_id": "e4"})
        ingestor.close(handle, {"status": "success"})

        types = [event.event_type.value for event in adapter_runtime.get_events(handle.run_id)]
        assert types == [
            "TASK_START",
            "TOOL_CALL",
            "ERROR",
            "TOOL_RESULT",
            "RECOVERY_START",
            "RECOVERY_RESULT",
            "TASK_END",
        ]
        assert adapter_runtime.get_recoveries(handle.run_id)[0].success is True


class TestTheDebugSurface:
    def test_status_reports_registered_adapters_and_persisted_state(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 54: a Python API, not a dashboard."""
        adapter_runtime.adapter_registry.register(GenericAgentAdapter)
        handle = ingestor.open(GenericAgentAdapter(), {"session_id": "s1", "task": "t"})
        ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

        status = adapter_runtime.adapter_status()

        assert status["protocol_version"] == "1"
        assert status["sessions"] == 1
        assert status["events"] == 1
        assert status["unapplied_events"] == 0
        assert status["adapters"][0]["name"] == "aer-generic"
        assert "explicit_adoption_signal" in status["adapters"][0]["missing_capabilities"]

    def test_sessions_and_events_are_listable(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), {"session_id": "s1", "task": "t"})
        ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

        sessions = adapter_runtime.adapter_sessions.list()
        events = adapter_runtime.adapter_events.list(aer_run_id=handle.run_id)

        assert [item.external_session_id for item in sessions] == ["s1"]
        assert [item.external_event_id for item in events] == ["e1"]


class TestAPreExistingExperienceIsUntouched:
    def test_the_adapter_only_adds_usage_and_events(
        self, ingestor: AdapterIngestor, adapter_runtime: AER, index
    ) -> None:
        """Distillation, promotion and confidence stay AER's; adapters do not touch them."""
        experience = store_experience(adapter_runtime, experience_id="exp-403")
        before = adapter_runtime.get_experience("exp-403")

        agent = StructuredAgent(ingestor, StructuredStyleAdapter(), session_id="s-403")
        agent.connect()
        agent.call("http.request")
        agent.outcome("http.request", ok=True)
        agent.finish(outcome="success")

        after = adapter_runtime.get_experience("exp-403")
        assert experience.status.value == "VERIFIED"
        assert after == before
