"""Milestone 4 acceptance: an agent that is wrong about its own success.

Implements the final acceptance scenario of the round-4 brief (sections 26-29, 34
and 43). The story:

.. code-block:: text

    Run Start
      -> TOOL_CALL wordpress.update_page
      -> TOOL_RESULT success            (the agent believes it worked)
      -> TASK_END                       Run.status = SUCCESS
      -> VERIFICATION http_status       HTTP 200        -> PASS
      -> VERIFICATION h1_count          expected 1, got 0 -> FAIL

The system must end up saying, simultaneously and without contradiction:

.. code-block:: text

    Agent execution:    SUCCESS
    Verification:       FAILED
    Verified success:   FALSE

``Run.status`` is **not** rewritten to ``FAILED``. The agent did do what it set out
to do -- that is an execution fact. The environment does not meet the requirement --
that is a verification fact. AER stores both, and it is exactly this pair that lets
the Experience Distiller (Milestone 5) know which "successful" trajectories must not
be learned from.

Every assertion is re-run against a freshly opened database, because a distinction
that does not survive a restart is not a durable fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aer import (
    AER,
    EventType,
    H1CountVerifier,
    HttpStatusVerifier,
    PredicateVerifier,
    RunStatus,
    VerificationContext,
    VerificationRecord,
    VerificationResult,
    VerifierType,
)

TOOL = "wordpress.update_page"
TASK = "Fix the WordPress product page H1"

HTTP_200 = {"actual_status": 200}
H1_MISSING = {"actual_count": 0}
H1_PRESENT = {"actual_count": 1}


def context_for(run_id: str, **payload: object) -> VerificationContext:
    return VerificationContext(run_id=run_id, task=TASK, payload=payload)  # type: ignore[arg-type]


def agent_updates_the_page(aer: AER) -> str:
    """A run where the agent does its job and declares success. Returns the run id."""
    run = aer.start_run(
        task=TASK,
        task_type="wordpress",
        agent_name="wp-agent",
        agent_version="0.2.0",
    )
    with run.tool(TOOL, input={"page_id": 123, "h1": "New H1"}) as tool:
        tool.set_result({"status": 200, "h1": "New H1"})
    run.success(final_score=1.0)
    return run.run_id


def http_check(required: bool = True) -> HttpStatusVerifier:
    return HttpStatusVerifier(expected_status=200, required=required)


def h1_check(required: bool = True) -> H1CountVerifier:
    return H1CountVerifier(expected_count=1, required=required)


def assert_the_disagreement_is_intact(aer: AER, run_id: str) -> None:
    """Every assertion the acceptance scenario makes, against a given runtime."""
    run = aer.get_run(run_id)
    assert run is not None

    # 1. The execution fact is untouched: the agent said SUCCESS and that stands.
    assert run.status is RunStatus.SUCCESS
    assert run.final_score == 1.0
    assert run.ended_at is not None

    # 2. The trace shows the work, then the verdicts, in one ordered sequence.
    events = aer.get_events(run_id)
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5, 6]
    assert [event.event_type for event in events] == [
        EventType.TASK_START,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TASK_END,
        EventType.VERIFICATION,
        EventType.VERIFICATION,
    ]

    # 3. The verification fact: one required check passed, one failed.
    verdicts = aer.get_verifications(run_id)
    assert [verdict.verifier_name for verdict in verdicts] == ["http_status", "h1_count"]
    assert [verdict.passed for verdict in verdicts] == [True, False]

    http_verdict, h1_verdict = verdicts
    assert http_verdict.verifier_type is VerifierType.DETERMINISTIC
    assert http_verdict.required is True
    assert http_verdict.result == {"actual_status": 200, "expected_status": 200}

    assert h1_verdict.result == {"actual_count": 0, "expected_count": 1}
    assert h1_verdict.message is not None and "0 <h1>" in h1_verdict.message

    # 4. Each verdict is anchored to the event that announced it.
    assert http_verdict.event_id == events[4].id
    assert h1_verdict.event_id == events[5].id
    assert events[5].output is not None
    assert events[5].output["verification_id"] == h1_verdict.id
    assert events[5].output["passed"] is False

    # 5. The combined judgement, recomputed from the stored facts.
    summary = aer.get_verification_summary(run_id)
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.failed == 1
    assert summary.required_failed == 1
    assert summary.pass_rate == 0.5
    assert summary.all_passed is False
    assert aer.verified_success(run_id) is False

    # 6. Nothing was invented to explain the failure away.
    assert aer.get_errors(run_id) == []


def test_agent_says_success_verifier_says_failed(data_dir: Path) -> None:
    """Section 26 and the final acceptance scenario, end to end."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        # The agent finished believing it succeeded.
        assert aer.get_verification_summary(run_id).total == 0
        assert aer.verified_success(run_id) is False

        # Verification happens afterwards -- exactly the production ordering.
        aer.verify(run_id, http_check(), context=context_for(run_id, **HTTP_200))
        aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_MISSING))

        assert_the_disagreement_is_intact(aer, run_id)
    finally:
        aer.close()


def test_the_disagreement_survives_a_restart(data_dir: Path) -> None:
    """Section 34: close the database, reopen it, and read the same two facts."""
    first = AER(data_dir)
    run_id = agent_updates_the_page(first)
    first.verify(run_id, http_check(), context=context_for(run_id, **HTTP_200))
    first.verify(run_id, h1_check(), context=context_for(run_id, **H1_MISSING))
    first.close()

    second = AER(data_dir)
    try:
        assert_the_disagreement_is_intact(second, run_id)
    finally:
        second.close()


def test_success_with_passing_verifiers_is_a_verified_success(data_dir: Path) -> None:
    """Section 27: the mirror case."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)

        aer.verify(run_id, http_check(), context=context_for(run_id, **HTTP_200))
        aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_PRESENT))

        run = aer.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.SUCCESS

        summary = aer.get_verification_summary(run_id)
        assert summary.total == 2
        assert summary.all_passed is True
        assert summary.pass_rate == 1.0
        assert aer.verified_success(run_id) is True
    finally:
        aer.close()


def test_a_failed_run_is_never_a_verified_success(data_dir: Path) -> None:
    """Section 28: passing checks prove the environment, not the agent's claim."""
    aer = AER(data_dir)
    try:
        run = aer.start_run(task=TASK, task_type="wordpress")
        with run.tool(TOOL, input={"page_id": 123}) as tool:
            tool.set_result({"status": 500})
        run.fail(final_score=0.0)

        aer.verify(run.run_id, http_check(), context=context_for(run.run_id, **HTTP_200))

        summary = aer.get_verification_summary(run.run_id)
        assert summary.all_passed is True
        assert aer.verified_success(run.run_id) is False

        stored = aer.get_run(run.run_id)
        assert stored is not None
        assert stored.status is RunStatus.FAILED
    finally:
        aer.close()


def test_multiple_verifiers_agreeing(data_dir: Path) -> None:
    """Section 29, first half: HTTP, H1 and schema all pass."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        schema = PredicateVerifier(
            name="schema_valid",
            predicate=lambda ctx: ctx.payload.get("schema_ok") is True,
        )

        payload = {**HTTP_200, **H1_PRESENT, "schema_ok": True}
        aer.verify(run_id, http_check(), context=context_for(run_id, **payload))
        aer.verify(run_id, h1_check(), context=context_for(run_id, **payload))
        aer.verify(run_id, schema, context=context_for(run_id, **payload))

        summary = aer.get_verification_summary(run_id)
        assert (summary.total, summary.passed, summary.failed) == (3, 3, 0)
        assert aer.verified_success(run_id) is True
    finally:
        aer.close()


def test_multiple_verifiers_disagreeing(data_dir: Path) -> None:
    """Section 29, second half: one failure is enough to withhold the confirmation."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        schema = PredicateVerifier(
            name="schema_valid",
            predicate=lambda ctx: ctx.payload.get("schema_ok") is True,
        )

        passing = {**HTTP_200, "schema_ok": True, **H1_MISSING}
        aer.verify(run_id, http_check(), context=context_for(run_id, **passing))
        aer.verify(run_id, h1_check(), context=context_for(run_id, **passing))
        aer.verify(run_id, schema, context=context_for(run_id, **passing))

        summary = aer.get_verification_summary(run_id)
        assert (summary.total, summary.passed, summary.failed) == (3, 2, 1)
        assert summary.required_failed == 1
        assert aer.verified_success(run_id) is False

        run = aer.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.SUCCESS
    finally:
        aer.close()


def test_an_optional_failure_does_not_veto_the_task(data_dir: Path) -> None:
    """Section 25: only required verdicts gate a verified success."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        quality = PredicateVerifier(
            name="llm_quality",
            required=False,
            predicate=lambda ctx: VerificationResult(
                passed=False, score=0.4, message="the copy reads awkwardly"
            ),
        )

        aer.verify(run_id, http_check(), context=context_for(run_id, **HTTP_200))
        aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_PRESENT))
        aer.verify(run_id, quality, context=context_for(run_id, **H1_PRESENT))

        summary = aer.get_verification_summary(run_id)
        assert summary.failed == 1
        assert summary.all_passed is False
        assert summary.required_failed == 0
        assert summary.all_required_passed is True
        assert aer.verified_success(run_id) is True
    finally:
        aer.close()


def test_verification_never_rewrites_the_agent_s_own_history(data_dir: Path) -> None:
    """The run row is byte-identical before and after a failing verdict."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        before = aer.get_run(run_id)
        assert before is not None

        aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_MISSING))

        after = aer.get_run(run_id)
        assert after is not None
        assert after == before
        assert after.status is RunStatus.SUCCESS
    finally:
        aer.close()


def test_two_runs_keep_their_own_verdicts(data_dir: Path) -> None:
    aer = AER(data_dir)
    try:
        good = agent_updates_the_page(aer)
        bad = agent_updates_the_page(aer)

        aer.verify(good, h1_check(), context=context_for(good, **H1_PRESENT))
        aer.verify(bad, h1_check(), context=context_for(bad, **H1_MISSING))

        assert aer.verified_success(good) is True
        assert aer.verified_success(bad) is False
        assert aer.get_verification_summary(good).passed == 1
        assert aer.get_verification_summary(bad).failed == 1
    finally:
        aer.close()


def test_a_verdict_is_immutable_once_recorded(data_dir: Path) -> None:
    """A verdict is an observation at a point in time; re-checking makes a new one."""
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        first = aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_MISSING))
        second = aer.verify(run_id, h1_check(), context=context_for(run_id, **H1_PRESENT))

        assert first.id != second.id
        assert first.passed is False
        assert second.passed is True

        stored_first = aer.verifications.get(first.id)
        assert isinstance(stored_first, VerificationRecord)
        assert stored_first.passed is False

        assert aer.get_verification_summary(run_id).total == 2
        assert aer.verified_success(run_id) is False
    finally:
        aer.close()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"actual_count": 0}, False),
        ({"actual_count": 1}, True),
        ({"actual_count": 2}, False),
    ],
)
def test_h1_count_boundary(payload: dict[str, int], expected: bool, data_dir: Path) -> None:
    aer = AER(data_dir)
    try:
        run_id = agent_updates_the_page(aer)
        verdict = aer.verify(run_id, h1_check(), context=context_for(run_id, **payload))

        assert verdict.passed is expected
        assert aer.verified_success(run_id) is expected
    finally:
        aer.close()
