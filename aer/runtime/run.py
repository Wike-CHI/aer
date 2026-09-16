"""The live handle an agent uses while it executes one task.

``RunContext`` is the only object business code needs:

.. code-block:: python

    run = aer.start_run(task="Fix WordPress H1", task_type="wordpress")

    run.emit(EventType.MODEL_CALL, input={"prompt": "..."})

    with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
        tool.set_result(update_page())

    try:
        risky_call()
    except PermissionError as exc:
        error = run.error(exc)
        with run.recovery(reason="REST API 403", error_id=error.id):
            fix_permissions()

    run.success()

    # Verification may happen before or after the run ends -- it is an observation,
    # not part of the task.
    run.verify(HttpStatusVerifier(expected_status=200), context=live_context)

It owns the run's *state machine*:

``RUNNING`` --success/partial_success/fail/abort--> terminal status

Invariants enforced here rather than trusted to the caller:

* a run can only be finished once;
* no *agent mutation* may follow the terminal status: no ``emit``, no hook, no
  ``error``. Those describe what the agent did, and the agent is done;
* **system observations are not agent mutations.** A ``VERIFICATION`` (or a human
  feedback signal) may still be appended after the run ends, because a real
  deployment verifies *after* the agent finishes. The whitelist lives in
  :data:`_SYSTEM_OBSERVATION_EVENTS` and is reachable only through the internal
  :meth:`_append_system_event`; there is no public "append any event after the
  end" door (round-4 brief, sections 31-33);
* every event -- hooks included -- goes through the same sequence-allocation path,
  whether it was appended before or after the terminal status;
* failures are recorded, never swallowed, and never automatically fail the run:
  a tool error followed by a successful recovery is a legitimate ``SUCCESS`` run.

Timing note: durations are measured with :func:`time.perf_counter` (monotonic),
while timestamps remain timezone-aware wall-clock datetimes. The two serve
different purposes and must not be conflated -- the system clock may jump.

Note what ``success()`` does **not** mean. It records that the agent runtime
declared the task complete. It is not a verification verdict: an independent
verifier may still disagree (agent.md #18, TASKS.md Task 4.5). The distinction is
kept visible by storing verdicts as separate ``VerificationRecord`` objects, and by
never rewriting ``Run.status`` when one of them fails (Milestone 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import TYPE_CHECKING

from aer.exceptions import RunStateError
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.hooks import RecoveryContext, ToolContext
from aer.runtime.models import ErrorRecord, Event, Experience, Run, VerificationRecord
from aer.runtime.sanitization import format_exception, redact
from aer.runtime.serialization import to_json_object, to_json_value, utc_now

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from aer.runtime.runtime import AER
    from aer.verification.base import VerificationContext, Verifier
    from aer.verification.summary import VerificationSummary

#: Statuses that may terminate a run. ``RUNNING`` is deliberately excluded.
_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCESS,
        RunStatus.PARTIAL_SUCCESS,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }
)

#: Events that may still be appended once a run reached a terminal status.
#:
#: These are *observations about* the run made by the system, not actions taken by
#: the agent, so the terminal guard -- which exists to stop the agent mutating a
#: finished record -- does not apply to them.
#:
#: ``ERROR`` is in the list for exactly one reason: when a verifier crashes, that
#: failure has to be recorded on the same trace, and it may well happen after the
#: agent finished. It is reachable only through the verification engine's internal
#: error path; the agent-facing ``run.error()`` remains blocked after the end.
_SYSTEM_OBSERVATION_EVENTS = frozenset(
    {
        EventType.VERIFICATION,
        EventType.HUMAN_FEEDBACK,
        EventType.ERROR,
    }
)


def _elapsed_ms(started_at: float) -> int:
    """Whole milliseconds since a :func:`time.perf_counter` reading."""
    return int((perf_counter() - started_at) * 1000)


class RunContext:
    """Mutable, persistence-backed handle over a single :class:`Run`."""

    def __init__(self, runtime: AER, run: Run) -> None:
        self._runtime = runtime
        self._run = run
        self._finished = run.is_finished
        # Monotonic reference for durations. Never derived from `started_at`.
        self._started_at = perf_counter()

    # -- read-only view ----------------------------------------------------

    @property
    def run(self) -> Run:
        """The current domain snapshot of this run."""
        return self._run

    @property
    def run_id(self) -> str:
        """Identifier of the underlying run."""
        return self._run.id

    @property
    def status(self) -> RunStatus:
        """Current status, refreshed by every state transition."""
        return self._run.status

    @property
    def is_finished(self) -> bool:
        """``True`` once a terminal status has been recorded."""
        return self._finished

    @property
    def runtime(self) -> AER:
        """The owning runtime.

        Exposed so that hook contexts can reach the repositories they own; a hook
        never touches storage directly.
        """
        return self._runtime

    def __repr__(self) -> str:
        return (
            f"RunContext(run_id={self.run_id!r}, status={self._run.status.value!r}, "
            f"finished={self._finished})"
        )

    # -- mutation guard ----------------------------------------------------

    def _require_running(self, action: str) -> None:
        """Reject an *agent mutation* on a run that already reached a terminal status.

        Called at the point of the mistake rather than when a hook is finally
        entered: ``run.tool(...)`` raising immediately is far easier to debug than
        a ``RunStateError`` surfacing three lines later inside a ``with`` block --
        or never, if the block is never entered.

        Raises:
            RunStateError: the run is finished.
        """
        if self._finished:
            raise RunStateError(
                f"Run {self.run_id} is already finished ({self._run.status.value}); cannot {action}"
            )

    # -- event capture -----------------------------------------------------

    def emit(
        self,
        event_type: EventType,
        *,
        input: object | None = None,
        output: object | None = None,
        duration_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Event:
        """Append a standardised event to this run and return it persisted.

        ``input``/``output`` accept any Python object; it is normalised into the
        JSON contract before storage, so a tool returning a non-JSON object still
        leaves a usable trace instead of losing the event. An object AER cannot
        represent becomes an explicit fallback marker rather than a plain string.

        Raises:
            RunStateError: the run has already reached a terminal status.
            StorageError: the event could not be persisted.
        """
        self._require_running(f"emit {EventType(event_type).value}")

        return self._runtime.events.create(
            Event(
                run_id=self._run.id,
                event_type=event_type,
                input=to_json_value(input),
                output=to_json_value(output),
                duration_ms=duration_ms,
                metadata=to_json_object(metadata),
            )
        )

    # -- hooks -------------------------------------------------------------

    def tool(
        self,
        name: str,
        *,
        input: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ToolContext:
        """Open a tool hook: ``TOOL_CALL`` on entry, ``TOOL_RESULT`` on exit.

        On exception the failure is recorded as ``ERROR`` (through the same
        pipeline as :meth:`error`) before ``TOOL_RESULT`` with ``success=false``,
        and the exception is then re-raised unchanged.

        Example::

            with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
                tool.set_result(update_page())

        Args:
            name: Tool identifier, recorded verbatim -- AER never interprets it.
            input: The arguments the agent passed, normalised for JSON.
            metadata: Extra JSON metadata attached to both events and any error.

        Raises:
            RunStateError: the run has already reached a terminal status.
        """
        self._require_running("open a tool hook")
        return ToolContext(self, name, input=input, metadata=metadata)

    def recovery(
        self,
        reason: str,
        *,
        error_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RecoveryContext:
        """Open a recovery hook: ``RECOVERY_START`` on entry, ``RECOVERY_RESULT`` on exit.

        When ``error_id`` names an error of this run and the block completes
        normally, that error is marked resolved. A failed attempt resolves
        nothing. The link is always explicit -- a later task succeeding never
        retroactively resolves history.

        Raises:
            RecordNotFoundError: ``error_id`` names no known error.
            AERError: ``error_id`` belongs to a different run.
            RunStateError: the run has already reached a terminal status.

        Example::

            with run.recovery(reason="REST API 403", error_id=error.id):
                fix_permissions()
        """
        self._require_running("open a recovery hook")
        return RecoveryContext(self, reason, error_id=error_id, metadata=metadata)

    def error(
        self,
        exc: BaseException,
        *,
        recoverable: bool = True,
        metadata: Mapping[str, object] | None = None,
    ) -> ErrorRecord:
        """Record a failure as an ``ERROR`` event plus a structured ``ErrorRecord``.

        This is the single error pipeline in AER. Tool and recovery hooks call the
        same private implementation, so there is exactly one place where failure
        shape, stack-trace handling and storage are decided (round-3 brief,
        section 14).

        The exception is *recorded*, not handled: this method returns normally, and
        it never marks the run failed. An agent may recover and still succeed.

        Args:
            exc: The exception to record. Any ``BaseException``, including
                ``KeyboardInterrupt``/``SystemExit``.
            recoverable: Whether the caller believes the failure can be repaired.
            metadata: Extra JSON metadata attached to the event and the record.

        Returns:
            The persisted :class:`~aer.runtime.models.ErrorRecord`, whose ``id``
            can be passed to :meth:`recovery`.

        Raises:
            RunStateError: the run has already reached a terminal status.
            StorageError: the event or the record could not be persisted.
        """
        return self._record_error(exc, recoverable=recoverable, metadata=metadata)

    def _record_error(
        self,
        exc: BaseException,
        *,
        recoverable: bool,
        metadata: Mapping[str, object] | None,
        system: bool = False,
    ) -> ErrorRecord:
        """The one and only error pipeline.

        Writes ``ERROR`` first, then the record that points back at it, so an
        ErrorRecord always has its event (round-3 brief, section 12).

        ``system`` selects the append path: ``False`` (= the agent reporting its own
        failure) obeys the terminal guard, ``True`` (= the runtime reporting a
        verifier crash) does not. Both paths allocate the sequence through the same
        repository call, so the trace stays contiguous either way.
        """
        exc_type = type(exc)
        error_type = f"{exc_type.__module__}.{exc_type.__qualname__}"
        # Both the message and the traceback are redacted: a credential leaks just
        # as easily through `str(exc)` (e.g. from a failing HTTP call) as through
        # the stack. Full-payload sanitisation is the later Sanitizer milestone.
        error_message = redact(str(exc) or repr(exc))
        stack_trace = format_exception(exc)
        meta = to_json_object(metadata)
        payload = {
            "error_type": error_type,
            "error_message": error_message,
            "recoverable": recoverable,
        }

        if system:
            event = self._append_system_event(EventType.ERROR, input=payload, metadata=meta)
        else:
            event = self.emit(EventType.ERROR, input=payload, metadata=meta)

        return self._runtime.errors.create(
            ErrorRecord(
                run_id=self._run.id,
                event_id=event.id,
                error_type=error_type,
                error_message=error_message,
                stack_trace=stack_trace,
                recoverable=recoverable,
                metadata=meta,
            )
        )

    # -- system observations -----------------------------------------------

    def _append_system_event(
        self,
        event_type: EventType,
        *,
        input: object | None = None,
        output: object | None = None,
        duration_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Event:
        """Append an observation *about* a run, possibly after it has ended.

        Internal API. Not exposed to business code, and not a general escape hatch:
        ``event_type`` must be in :data:`_SYSTEM_OBSERVATION_EVENTS`, so there is no
        way to slip a ``TOOL_CALL`` in after the run is over and no way to bypass
        the ordering rules with a free-form event.

        The difference from :meth:`emit` is exactly the missing terminal check --
        which is the point: a production verifier runs after the agent finished.
        Everything else is identical, including the sequence allocation, so a late
        observation still lands in the one ordered trace.

        Raises:
            RunStateError: ``event_type`` is not a system observation event.
            StorageError: the event could not be persisted.
        """
        if event_type not in _SYSTEM_OBSERVATION_EVENTS:
            raise RunStateError(
                f"{EventType(event_type).value} is not a system observation event; "
                "only VERIFICATION, HUMAN_FEEDBACK and AER-internal errors may be "
                "appended to a finished run"
            )

        return self._runtime.events.create(
            Event(
                run_id=self._run.id,
                event_type=event_type,
                input=to_json_value(input),
                output=to_json_value(output),
                duration_ms=duration_ms,
                metadata=to_json_object(metadata),
            )
        )

    # -- verification ------------------------------------------------------

    def verify(
        self,
        verifier: Verifier,
        *,
        context: VerificationContext | None = None,
        required: bool | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> VerificationRecord:
        """Run an independent verifier and record its verdict.

        Usable before *and* after :meth:`success` / :meth:`fail`: verification is an
        observation of the world, and in a real deployment it happens after the
        agent is done. Verifying never changes ``Run.status`` -- an agent that
        declared success keeps that declaration even when the verifier disagrees,
        which is the whole point of the distinction.

        Example::

            run.verify(
                HttpStatusVerifier(expected_status=200),
                context=VerificationContext(
                    run_id=run.run_id, payload={"actual_status": 200},
                ),
            )

        Raises:
            VerificationError: not a verifier, or it returned a non-verdict.
            AERError: ``context`` belongs to a different run.
            BaseException: whatever the verifier raised, after it was recorded.
            StorageError: the event or the record could not be persisted.
        """
        return self._runtime.verification_engine.verify(
            self, verifier, context=context, required=required, metadata=metadata
        )

    def get_verification_summary(self) -> VerificationSummary:
        """Aggregate the verdicts recorded for this run."""
        return self._runtime.verification_engine.summarize(self.run_id)

    def verified_success(self) -> bool:
        """Whether this run is an agent success **and** independently confirmed.

        A derived judgement, recomputed on every call -- never stored on the run
        (round-4 brief, section 24).
        """
        return self._runtime.verification_engine.verified_success(self.run_id)

    # -- experience --------------------------------------------------------

    def distill(self, *, explicit_high_value: bool = False) -> Experience | None:
        """Distil this run into reusable knowledge, or ``None`` if not worth it.

        Usable after the run finished -- distillation is post-processing, like
        verification -- and idempotent, so calling it twice returns the same
        experience rather than a duplicate (round-5 brief, section 42).

        Raises:
            DistillationError: no provider is configured, or it returned something
                unusable. Nothing is persisted, and the run is left untouched.
        """
        return self._runtime.distill_run(self.run_id, explicit_high_value=explicit_high_value)

    # -- completion --------------------------------------------------------

    def success(
        self,
        *,
        final_score: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Run:
        """Declare the task complete, as judged by the agent runtime."""
        return self.finish(RunStatus.SUCCESS, final_score=final_score, metadata=metadata)

    def partial_success(
        self,
        *,
        final_score: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Run:
        """Declare the task partially complete."""
        return self.finish(RunStatus.PARTIAL_SUCCESS, final_score=final_score, metadata=metadata)

    def fail(
        self,
        *,
        final_score: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Run:
        """Declare the task failed."""
        return self.finish(RunStatus.FAILED, final_score=final_score, metadata=metadata)

    def abort(
        self,
        *,
        final_score: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Run:
        """Declare the task abandoned before completion."""
        return self.finish(RunStatus.ABORTED, final_score=final_score, metadata=metadata)

    def finish(
        self,
        status: RunStatus,
        *,
        final_score: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Run:
        """Move the run to a terminal ``status`` and record ``TASK_END``.

        Raises:
            RunStateError: the run is already finished, or ``status`` is not a
                terminal status.
            StorageError: the status change or the ``TASK_END`` event failed to
                persist.
        """
        if self._finished:
            raise RunStateError(
                f"Run {self.run_id} already finished with status {self._run.status.value}"
            )
        if status not in _TERMINAL_STATUSES:
            raise RunStateError(f"{status.value} is not a terminal run status")

        # Wall clock for the timestamp, monotonic clock for the duration.
        ended_at = utc_now()
        duration_ms = _elapsed_ms(self._started_at)

        run = self._run
        run.status = status
        run.ended_at = ended_at
        if final_score is not None:
            run.final_score = final_score
        if metadata is not None:
            run.metadata.update(to_json_object(metadata))

        # Persist the status transition first: if writing TASK_END fails, the run
        # is still stored with the correct terminal status.
        self._run = self._runtime.runs.update(run)

        self._runtime.events.create(
            Event(
                run_id=run.id,
                event_type=EventType.TASK_END,
                output=to_json_value(
                    {
                        "status": status.value,
                        "final_score": run.final_score,
                        "duration_ms": duration_ms,
                    }
                ),
                created_at=ended_at,
                duration_ms=duration_ms,
            )
        )
        self._finished = True
        return self._run
