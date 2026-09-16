"""Shared builders for the Milestone 5 tests.

Imported as ``tests.experience.support`` (the repository root is on ``sys.path`` via
``pythonpath = ["."]``). Kept out of ``conftest.py`` on purpose: these are ordinary
helpers that return values, not fixtures that own state.
"""

from __future__ import annotations

from collections.abc import Callable

from aer import (
    AER,
    EventType,
    ExperienceCandidate,
    ExperienceKind,
    H1CountVerifier,
    HttpStatusVerifier,
    VerificationContext,
)

DEFAULT_DOMAIN = "wordpress"


def candidate(**overrides: object) -> ExperienceCandidate:
    """A well-formed candidate, with any field overridable per test."""
    payload: dict[str, object] = {
        "domain": DEFAULT_DOMAIN,
        "title": "REST API 403 on page update",
        "problem": "Updating a page returns 403 and the H1 never changes",
        "symptoms": ("403 on POST /wp-json/wp/v2/pages/123", "H1 count stays 0"),
        "failed_attempts": ("blind retry", "re-sent the same Authorization header"),
        "root_cause": "missing edit_posts capability",
        "root_cause_confidence": 0.6,
        "solution": "switch to a credential with edit_posts",
        "recommended_workflow": ("check the capability before retrying",),
        "avoid": ("retrying without checking permissions",),
        "confidence_hint": 0.9,
    }
    payload.update(overrides)
    return ExperienceCandidate(**payload)  # type: ignore[arg-type]


class StaticProvider:
    """A provider that always returns the same candidate and records its calls.

    Satisfies the :class:`~aer.experience.provider.DistillationProvider` protocol
    structurally -- no AER base class needed, which is the point.
    """

    def __init__(
        self,
        result: ExperienceCandidate | None = None,
        *,
        name: str = "static",
        kind: ExperienceKind | None = None,
    ) -> None:
        self.name = name
        self._result = result
        self._kind = kind
        self.calls: list[object] = []

    def distill(self, evidence: object) -> ExperienceCandidate:
        self.calls.append(evidence)
        if self._result is not None:
            return self._result
        return candidate(kind=self._kind)


class CrashingProvider:
    """A provider that raises whatever it was given."""

    def __init__(self, error: BaseException | None = None, *, name: str = "broken") -> None:
        self.name = name
        self.error = error or TimeoutError("provider timed out")
        self.calls: list[object] = []

    def distill(self, evidence: object) -> ExperienceCandidate:
        self.calls.append(evidence)
        raise self.error


class SloppyProvider:
    """A provider that returns something that is not a candidate."""

    name = "sloppy"

    def distill(self, evidence: object) -> object:
        del evidence
        return {"title": "not a model instance"}


def context_for(run_id: str, **payload: object) -> VerificationContext:
    """A verification context carrying evidence payload."""
    return VerificationContext(run_id=run_id, payload=payload)  # type: ignore[arg-type]


def http_verifier(required: bool = True) -> HttpStatusVerifier:
    return HttpStatusVerifier(expected_status=200, required=required)


def h1_verifier(required: bool = True) -> H1CountVerifier:
    return H1CountVerifier(expected_count=1, required=required)


def build_recovery_run(
    aer: AER,
    *,
    task: str = "Fix the product page H1",
    verify: bool = True,
) -> str:
    """A run that failed, was repaired, and succeeded. Returns the run id.

    The canonical RECOVERY trajectory (round-5 brief section 53)::

        TOOL_CALL -> ERROR 403 -> TOOL_RESULT failed
        RECOVERY_START -> RECOVERY_RESULT success
        TOOL_CALL -> TOOL_RESULT success
        TASK_END (SUCCESS) -> VERIFICATION PASS
    """
    run = aer.start_run(task=task, task_type="wordpress")
    attempt = run.tool("wordpress.update_page", input={"page_id": 123})
    try:
        with attempt:
            raise PermissionError("403 Forbidden")
    except PermissionError:
        pass

    record = attempt.error_record
    assert record is not None
    with run.recovery(reason="REST API 403", error_id=record.id) as recovery:
        recovery.set_result({"action": "granted_edit_posts"})

    with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
        tool.set_result({"status": 200, "h1": "New H1"})
    run.success()

    if verify:
        payload = {"actual_status": 200, "actual_count": 1}
        run.verify(http_verifier(), context=context_for(run.run_id, **payload))
        run.verify(h1_verifier(), context=context_for(run.run_id, **payload))
    return run.run_id


def build_false_success_run(aer: AER, *, task: str = "Fix the H1 blind") -> str:
    """An agent that believed it succeeded while a verifier disagreed."""
    run = aer.start_run(task=task, task_type="wordpress")
    with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
        tool.set_result({"status": 200})
    run.success()
    run.verify(
        h1_verifier(),
        context=context_for(run.run_id, actual_status=200, actual_count=0),
    )
    return run.run_id


def build_unresolved_failure_run(aer: AER, *, task: str = "Fix the H1 and give up") -> str:
    """An agent that failed, tried to repair it, failed again and gave up."""
    run = aer.start_run(task=task, task_type="wordpress")
    attempt = run.tool("wordpress.update_page", input={"page_id": 123})
    try:
        with attempt:
            raise PermissionError("403 Forbidden")
    except PermissionError:
        pass

    record = attempt.error_record
    assert record is not None
    try:
        with run.recovery(reason="clear the cache", error_id=record.id):
            raise RuntimeError("still 403 after clearing the cache")
    except RuntimeError:
        pass
    run.fail()
    return run.run_id


def build_plain_success_run(aer: AER, *, task: str = "Trivial page edit") -> str:
    """The uneventful run the policy is supposed to ignore."""
    run = aer.start_run(task=task, task_type="wordpress")
    with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
        tool.set_result({"status": 200})
    run.success()
    run.verify(http_verifier(), context=context_for(run.run_id, actual_status=200))
    return run.run_id


def add_human_feedback(aer: AER, run_id: str, *, approved: bool = True) -> None:
    """Append a HUMAN_FEEDBACK system observation to a finished run."""
    run = aer.get_run(run_id)
    assert run is not None
    from aer.runtime.run import RunContext

    RunContext(aer, run)._append_system_event(
        EventType.HUMAN_FEEDBACK, output={"approved": approved, "reviewer": "ops-42"}
    )


#: Type alias for the provider factories the fixtures hand out.
ProviderFactory = Callable[..., object]
