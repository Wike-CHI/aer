"""The verification engine's pipeline (Milestone 4, sections 11-13, 36, 39).

What is pinned here is the *single path* guarantee: one call produces one
``VERIFICATION`` event, one ``VerificationRecord``, and the two point at each
other. Anything that records a verdict without doing all three has escaped the
pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import (
    AER,
    AERError,
    CallableEnvironmentVerifier,
    EventType,
    H1CountVerifier,
    HttpStatusVerifier,
    PredicateVerifier,
    RecordNotFoundError,
    RunStateError,
    VerificationContext,
    VerificationError,
    VerificationResult,
    VerifierType,
)
from aer.runtime.sanitization import MESSAGE_MAX_LENGTH, REDACTED

CONTEXT_PAYLOAD = {"actual_status": 200, "actual_count": 1}


def live_context(run_id: str, **payload: object) -> VerificationContext:
    """A context that carries enough evidence for the HTTP and H1 primitives."""
    merged: dict[str, object] = {**CONTEXT_PAYLOAD, **payload}
    return VerificationContext(run_id=run_id, payload=merged)  # type: ignore[arg-type]


class TestPipeline:
    def test_one_verify_produces_one_event_and_one_record(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        before = len(aer.get_events(context.run_id))

        record = context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )

        assert aer.verifications.count_by_run(context.run_id) == 1
        events = aer.get_events(context.run_id)
        assert len(events) == before + 1
        assert events[-1].event_type is EventType.VERIFICATION
        assert aer.verifications.get(record.id) == record

    def test_the_event_and_the_record_reference_each_other(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        record = context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )
        event = aer.get_events(context.run_id)[-1]

        assert record.event_id == event.id
        assert event.output is not None
        assert event.output["verification_id"] == record.id

    def test_the_event_payload_is_readable_without_the_table(self, aer: AER) -> None:
        """Section 13: a timeline reader must understand the verdict from the event."""
        context = aer.start_run(task="task")
        context.success()

        record = context.verify(
            H1CountVerifier(expected_count=1),
            context=live_context(context.run_id, actual_count=0),
        )
        payload = aer.get_events(context.run_id)[-1].output

        assert payload is not None
        assert payload["verifier_name"] == "h1_count"
        assert payload["verifier_type"] == "DETERMINISTIC"
        assert payload["required"] is True
        assert payload["passed"] is False
        assert payload["score"] is None
        assert payload["verification_id"] == record.id
        assert payload["message"] == record.message

    def test_structured_detail_stays_out_of_the_event(self, aer: AER) -> None:
        """Large verdict payloads belong in ``verifications``, not in the trace."""
        context = aer.start_run(task="task")
        context.success()
        big = {"detail": ["x" * 100] * 200}
        graded = PredicateVerifier(
            name="detail",
            predicate=lambda ctx: VerificationResult(passed=True, result=big),
        )

        record = context.verify(graded, context=live_context(context.run_id))
        payload = aer.get_events(context.run_id)[-1].output

        assert record.result == big
        assert payload is not None
        assert "result" not in payload
        assert "detail" not in payload

    def test_verifier_type_is_taken_from_the_verifier(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        record = context.verify(
            CallableEnvironmentVerifier(name="live", check=lambda ctx: True),
            context=live_context(context.run_id),
        )

        assert record.verifier_type is VerifierType.ENVIRONMENT

    def test_score_and_message_are_preserved(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        verifier = PredicateVerifier(
            name="graded",
            predicate=lambda ctx: VerificationResult(
                passed=False, score=0.4, message="3 of 5 checks passed"
            ),
        )

        record = context.verify(verifier, context=live_context(context.run_id))

        stored = aer.verifications.get(record.id)
        assert stored is not None
        assert stored.score == 0.4
        assert stored.message == "3 of 5 checks passed"

    def test_metadata_from_the_verifier_and_the_caller_are_merged(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        verifier = PredicateVerifier(
            name="tagged",
            predicate=lambda ctx: VerificationResult(passed=True, metadata={"engine": "h1"}),
        )

        record = context.verify(
            verifier,
            context=live_context(context.run_id),
            metadata={"sweep": "evening"},
        )

        assert record.metadata == {"engine": "h1", "sweep": "evening"}
        assert aer.get_events(context.run_id)[-1].metadata == {"sweep": "evening"}

    def test_duration_is_recorded_on_the_event(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )

        duration = aer.get_events(context.run_id)[-1].duration_ms
        assert isinstance(duration, int)
        assert duration >= 0

    def test_required_is_true_by_default_and_can_be_overridden(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        verifier = HttpStatusVerifier(expected_status=200)

        default = context.verify(verifier, context=live_context(context.run_id))
        optional = context.verify(verifier, context=live_context(context.run_id), required=False)

        assert default.required is True
        assert optional.required is False
        stored = aer.verifications.get(optional.id)
        assert stored is not None
        assert stored.required is False

    def test_a_failing_verdict_never_touches_the_run_status(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        context.verify(
            H1CountVerifier(expected_count=1),
            context=live_context(context.run_id, actual_count=0),
        )

        run = aer.get_run(context.run_id)
        assert run is not None
        assert run.status.value == "SUCCESS"
        assert aer.get_errors(context.run_id) == []


class TestOrdering:
    def test_verification_after_success_follows_task_end(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.TASK_END,
            EventType.VERIFICATION,
        ]
        assert [event.sequence for event in events] == [1, 2, 3]

    def test_verification_before_success_precedes_task_end(self, aer: AER) -> None:
        """Both lifecycles are legal; the sequence is contiguous either way."""
        context = aer.start_run(task="task")
        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )
        context.success()

        events = aer.get_events(context.run_id)
        assert [event.event_type for event in events] == [
            EventType.TASK_START,
            EventType.VERIFICATION,
            EventType.TASK_END,
        ]
        assert [event.sequence for event in events] == [1, 2, 3]

    def test_verification_sits_inside_a_tool_trace(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        with context.tool("wordpress.update_page") as tool:
            tool.set_result({"status": 200})
        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )
        context.success()

        assert [event.event_type for event in aer.get_events(context.run_id)] == [
            EventType.TASK_START,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.VERIFICATION,
            EventType.TASK_END,
        ]

    def test_repeated_verdicts_keep_their_order(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        for expected in (200, 403, 500):
            context.verify(
                HttpStatusVerifier(expected_status=expected, name=f"http_{expected}"),
                context=live_context(context.run_id),
            )

        verdicts = aer.get_verifications(context.run_id)
        names = [verdict.verifier_name for verdict in verdicts]
        assert names == ["http_200", "http_403", "http_500"]
        assert [verdict.passed for verdict in verdicts] == [True, False, False]

    def test_no_sequence_is_skipped_by_a_verdict(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.emit(EventType.MODEL_CALL)
        context.success()
        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )

        sequences = [event.sequence for event in aer.get_events(context.run_id)]
        assert sequences == list(range(1, len(sequences) + 1))


class TestContextHandling:
    def test_a_default_context_carries_the_run_id_and_task(self, aer: AER) -> None:
        context = aer.start_run(task="Fix the H1", task_type="wordpress")
        context.success()
        seen: list[VerificationContext] = []

        def predicate(ctx: VerificationContext) -> bool:
            seen.append(ctx)
            return True

        context.verify(PredicateVerifier(name="spy", predicate=predicate))

        assert len(seen) == 1
        assert seen[0].run_id == context.run_id
        assert seen[0].task == "Fix the H1"
        assert seen[0].artifacts == []
        assert seen[0].payload == {}

    def test_a_context_for_another_run_is_rejected_before_anything_is_written(
        self, aer: AER
    ) -> None:
        context = aer.start_run(task="task")
        context.success()
        before = aer.get_events(context.run_id)

        with pytest.raises(AERError, match="belongs to run"):
            context.verify(
                HttpStatusVerifier(expected_status=200),
                context=live_context("some-other-run"),
            )

        assert aer.get_events(context.run_id) == before
        assert aer.verifications.count_by_run(context.run_id) == 0
        assert aer.verifications.count_by_run("some-other-run") == 0

    def test_an_object_that_is_not_a_verifier_is_rejected(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(VerificationError, match="does not implement the Verifier protocol"):
            context.verify("http_status")  # type: ignore[arg-type]

        assert aer.verifications.count_by_run(context.run_id) == 0

    def test_a_verifier_returning_a_non_verdict_is_treated_as_a_crash(self, aer: AER) -> None:
        class Sloppy:
            name = "sloppy"
            verifier_type = VerifierType.DETERMINISTIC
            required = True

            def verify(self, context: VerificationContext) -> object:
                del context
                return "passed, honest"

        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(VerificationError, match="must return a VerificationResult"):
            context.verify(Sloppy())  # type: ignore[arg-type]

        # A malformed verdict reaches neither storage nor the caller as a verdict.
        assert aer.verifications.count_by_run(context.run_id) == 0
        assert aer.get_events(context.run_id)[-1].event_type is EventType.ERROR


class TestSanitisation:
    def test_a_credential_in_a_verifier_message_is_redacted(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        verifier = PredicateVerifier(
            name="leaky",
            predicate=lambda ctx: VerificationResult(
                passed=False,
                message="request failed: Authorization: Bearer sk-live-ABCDEF123456",
            ),
        )

        record = context.verify(verifier, context=live_context(context.run_id))

        assert record.message is not None
        assert "sk-live-ABCDEF123456" not in record.message
        assert REDACTED in record.message
        # The event carries the same sanitised text, so the two never disagree.
        payload = aer.get_events(context.run_id)[-1].output
        assert payload is not None
        assert payload["message"] == record.message

    def test_an_enormous_message_is_truncated(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        verifier = PredicateVerifier(
            name="verbose",
            predicate=lambda ctx: VerificationResult(passed=False, message="x" * 50_000),
        )

        record = context.verify(verifier, context=live_context(context.run_id))

        assert record.message is not None
        assert len(record.message) <= MESSAGE_MAX_LENGTH
        assert "truncated" in record.message

    def test_structured_result_payloads_are_not_rewritten(self, aer: AER) -> None:
        """Same boundary as Milestone 3 (D-020): evidence is not rewritten in place."""
        context = aer.start_run(task="task")
        context.success()
        evidence = {"headers": {"authorization": "Bearer sk-live-ABCDEF123456"}}
        verifier = PredicateVerifier(
            name="evidence",
            predicate=lambda ctx: VerificationResult(passed=True, result=evidence),
        )

        record = context.verify(verifier, context=live_context(context.run_id))

        assert record.result == evidence


class TestFacadeEntryPoint:
    def test_a_run_can_be_verified_by_id_after_the_fact(self, aer: AER) -> None:
        """The realistic ordering: the agent process is gone, verification comes later."""
        context = aer.start_run(task="task")
        context.success()
        run_id = context.run_id

        record = aer.verify(
            run_id,
            HttpStatusVerifier(expected_status=200),
            context=live_context(run_id),
        )

        assert record.run_id == run_id
        assert aer.verifications.count_by_run(run_id) == 1

    def test_both_entry_points_share_one_pipeline(self, aer: AER) -> None:
        """``AER.verify`` must not be a second implementation (section 11)."""
        context = aer.start_run(task="task")
        context.success()
        run_id = context.run_id

        from_context = context.verify(
            HttpStatusVerifier(expected_status=200, name="via_context"),
            context=live_context(run_id),
        )
        from_facade = aer.verify(
            run_id,
            HttpStatusVerifier(expected_status=200, name="via_facade"),
            context=live_context(run_id),
        )

        assert from_context.event_id is not None
        assert from_facade.event_id is not None
        assert from_context.event_id != from_facade.event_id
        assert aer.verifications.count_by_run(run_id) == 2
        assert [v.verifier_name for v in aer.verifications.get_by_run(run_id)] == [
            "via_context",
            "via_facade",
        ]

    def test_verifying_an_unknown_run_raises(self, aer: AER) -> None:
        with pytest.raises(RecordNotFoundError, match="Run not found"):
            aer.verify(
                "no-such-run",
                HttpStatusVerifier(expected_status=200),
                context=live_context("no-such-run"),
            )

    def test_get_verifications_filters(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()
        context.verify(
            HttpStatusVerifier(expected_status=200),
            context=live_context(context.run_id),
        )
        context.verify(
            HttpStatusVerifier(expected_status=500, name="http_500"),
            context=live_context(context.run_id),
            required=False,
        )

        assert len(aer.get_verifications(context.run_id)) == 2
        assert len(aer.get_verifications(context.run_id, passed=True)) == 1
        assert len(aer.get_verifications(context.run_id, required=False)) == 1

    def test_operations_on_a_closed_runtime_fail(self, data_dir: Path) -> None:
        runtime = AER(data_dir)
        context = runtime.start_run(task="task")
        context.success()
        runtime.close()

        with pytest.raises(AERError, match="already closed"):
            runtime.get_verification_summary(context.run_id)

    def test_the_runtime_guard_still_blocks_agent_mutations(self, aer: AER) -> None:
        context = aer.start_run(task="task")
        context.success()

        with pytest.raises(RunStateError):
            context.emit(EventType.MODEL_CALL)
