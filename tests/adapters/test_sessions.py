"""Sessions: open, resume, reopen, and survive a restart (sections 20-22, 43, 50-51).

The behaviour that matters here is what happens on the *third* connect. The first
connect is obvious; the second one is where an integration either resumes the run it was
reporting into or silently starts a second trace for the same work. The third -- after
the work already finished -- is the case the brief insists must be decided rather than
improvised (section 43).
"""

from __future__ import annotations

import pytest

from aer import AER, AdapterSessionOutcome, GenericAgentAdapter, RunStatus
from aer.adapter import AdapterIngestor
from aer.exceptions import AdapterProtocolError, AdapterSessionTerminated
from aer.runtime.enums import RunStatus as RuntimeRunStatus

SESSION = {"session_id": "conversation-7", "task": "fix the REST API 403"}


class TestStartingAndResuming:
    def test_a_new_session_starts_a_run(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        assert handle.outcome is AdapterSessionOutcome.STARTED
        run = adapter_runtime.get_run(handle.run_id)
        assert run is not None
        assert run.task_description == SESSION["task"]
        assert run.status is RunStatus.RUNNING

    def test_the_run_carries_the_adapter_identity(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 38: "which integration produced this?" must be answerable later."""
        handle = ingestor.open(
            GenericAgentAdapter(model="some-model", agent_version="9.1"), SESSION
        )

        metadata = adapter_runtime.get_run(handle.run_id).metadata
        assert metadata["adapter"] == {
            "name": "aer-generic",
            "version": "1",
            "protocol_version": "1",
        }
        assert metadata["provider"] == "generic"
        assert metadata["agent"]["model"] == "some-model"
        assert metadata["agent"]["version"] == "9.1"
        assert metadata["external_session_id"] == "conversation-7"

    def test_reconnecting_resumes_the_run_in_progress(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 21: a restart must not open a second trace for the same work."""
        first = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.ingest(first, {"type": "tool_call", "tool": "shell", "event_id": "e1"})

        second = ingestor.open(GenericAgentAdapter(), SESSION)

        assert second.outcome is AdapterSessionOutcome.RESUMED
        assert second.run_id == first.run_id
        assert adapter_runtime.list_runs() and len(adapter_runtime.list_runs()) == 1

    def test_the_mapping_is_persisted_with_the_vendor_identifiers(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        session = adapter_runtime.find_adapter_session("generic", "conversation-7")
        assert session is not None
        assert session.aer_run_id == handle.run_id
        assert session.adapter_name == "aer-generic"
        assert session.protocol_version == "1"

    def test_two_different_external_sessions_get_two_runs(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        first = ingestor.open(GenericAgentAdapter(), SESSION)
        second = ingestor.open(
            GenericAgentAdapter(), {"session_id": "conversation-8", "task": "other"}
        )

        assert first.run_id != second.run_id
        assert adapter_runtime.adapter_sessions.count() == 2


class TestAfterTheRunFinished:
    def test_reopening_a_finished_session_is_refused_by_default(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 43: refuse, do not improvise. Resume and a new episode differ."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.close(handle, {"status": "success"})

        with pytest.raises(AdapterSessionTerminated, match="already mapped"):
            ingestor.open(GenericAgentAdapter(), SESSION)

        assert len(adapter_runtime.list_runs()) == 1

    def test_reopen_starts_a_second_episode_and_keeps_the_first(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        first = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.close(first, {"status": "success"})

        second = ingestor.reopen(GenericAgentAdapter(), SESSION)

        assert second.outcome is AdapterSessionOutcome.REOPENED
        assert second.run_id != first.run_id
        assert adapter_runtime.get_run(first.run_id) is not None
        session = adapter_runtime.find_adapter_session("generic", "conversation-7")
        assert session is not None
        assert session.aer_run_id == second.run_id
        assert session.previous_runs == (first.run_id,)

    def test_an_event_after_the_terminal_status_is_still_guarded(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """Section 22: the adapter cannot bypass the runtime's terminal guard."""
        from aer.exceptions import RunStateError

        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        ingestor.close(handle, {"status": "success"})

        with pytest.raises(RunStateError, match="already finished"):
            ingestor.ingest(handle, {"type": "tool_call", "tool": "shell", "event_id": "late"})

    def test_closing_an_already_finished_run_is_a_no_op(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """A duplicated session-end delivery must not fail a hook."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)
        first = ingestor.close(handle, {"status": "success"})

        second = ingestor.close(handle, {"status": "failed"})

        assert first is not None and second is not None
        assert second.status is RunStatus.SUCCESS


class TestClosing:
    def test_an_unknown_status_leaves_the_run_running(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        """ "The session disconnected" is not "the task ended"."""
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        result = ingestor.close(handle, {"status": "wrapped up"})

        assert result is None
        assert adapter_runtime.get_run(handle.run_id).status is RuntimeRunStatus.RUNNING

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("success", RuntimeRunStatus.SUCCESS),
            ("completed", RuntimeRunStatus.SUCCESS),
            ("failed", RuntimeRunStatus.FAILED),
            ("cancelled", RuntimeRunStatus.ABORTED),
            ("partial", RuntimeRunStatus.PARTIAL_SUCCESS),
            ("unknown", RuntimeRunStatus.INCONCLUSIVE),
            ("inconclusive", RuntimeRunStatus.INCONCLUSIVE),
        ],
    )
    def test_the_status_word_is_translated(
        self,
        ingestor: AdapterIngestor,
        adapter_runtime: AER,
        word: str,
        expected: RuntimeRunStatus,
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.close(handle, {"status": word})

        assert adapter_runtime.get_run(handle.run_id).status is expected

    def test_the_close_reason_is_kept(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        handle = ingestor.open(GenericAgentAdapter(), SESSION)

        ingestor.close(handle, {"status": "success", "reason": "the page was fixed"})

        run = adapter_runtime.get_run(handle.run_id)
        assert run.metadata["adapter_close_reason"] == "the page was fixed"


class TestRestartPersistence:
    def test_a_reconnect_after_a_process_restart_resumes(self, data_dir, index) -> None:
        """Sections 50-51: the mapping has to outlive the process that wrote it."""
        with AER(data_dir, knowledge_index=index) as first:
            adapter = GenericAgentAdapter()
            handle = first.adapter_ingestor.open(adapter, SESSION)
            first.adapter_ingestor.ingest(
                handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"}
            )
            run_id = handle.run_id

        with AER(data_dir, knowledge_index=index) as second:
            resumed = second.adapter_ingestor.open(GenericAgentAdapter(), SESSION)

            assert resumed.outcome is AdapterSessionOutcome.RESUMED
            assert resumed.run_id == run_id
            # And the events written before the restart are still on that run.
            types = [event.event_type.value for event in second.get_events(run_id)]
            assert types == ["TASK_START", "TOOL_CALL"]

    def test_the_ledger_survives_a_restart_too(self, data_dir, index) -> None:
        """Otherwise a retry across a restart would be applied a second time."""
        raw = {"type": "tool_call", "tool": "shell", "event_id": "evt-1"}
        with AER(data_dir, knowledge_index=index) as first:
            handle = first.adapter_ingestor.open(GenericAgentAdapter(), SESSION)
            first.adapter_ingestor.ingest(handle, raw)

        with AER(data_dir, knowledge_index=index) as second:
            handle = second.adapter_ingestor.open(GenericAgentAdapter(), SESSION)
            replayed = second.adapter_ingestor.ingest(handle, raw)

            assert replayed.outcome.value == "DUPLICATE"
            assert second.adapter_events.count() == 1


class TestOpenValidation:
    def test_an_unregistered_name_is_reported_with_the_registered_list(
        self, adapter_runtime: AER
    ) -> None:
        """The usual cause is a typo in a config file, so the message lists what is."""
        from aer.exceptions import AdapterError

        adapter_runtime.adapter_registry.register(GenericAgentAdapter)

        with pytest.raises(AdapterError, match=r"Registered: aer-generic"):
            adapter_runtime.open_adapter_session("nope", SESSION)

    def test_a_session_without_a_task_is_refused(
        self, ingestor: AdapterIngestor, adapter_runtime: AER
    ) -> None:
        with pytest.raises(AdapterProtocolError):
            ingestor.open(GenericAgentAdapter(), {"session_id": "s"})
