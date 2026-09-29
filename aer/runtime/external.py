"""Recording events that happened **outside** this process (Milestone 8).

Everything else in the runtime records what an agent did through AER's own SDK, in the
same process, at the moment it happened. An Agent adapter is different: it receives
events from another product's hook system, on another process's schedule, possibly out
of order and possibly more than once. This module is the narrow, explicit door those
events come through.

It exists so that the adapter layer never has to reach into the runtime's internals.
The adapter-facing alternative -- letting an adapter build events and hand them to the
repository -- would bypass three things that must not be bypassed (round-8 brief,
section 10): the run's terminal-state guard, the single error pipeline, and the
sequence allocation that keeps one trace contiguous. So an adapter gets these four
operations and nothing else.

Two differences from the agent-facing SDK, both deliberate:

**Failures come from primitives, not from exceptions.** An agent's failure is a Python
exception and AER records its traceback. An *externally reported* failure has a type
and a message but no traceback of ours; synthesising one from the adapter's own stack
would file AER's frames under the agent's name. ``failure()`` therefore records what
the source actually said (round-8 section 28), and truncates it, because the source is
untrusted and unbounded (section 27).

**Only an observation may arrive after the run ended.** A ``HUMAN_FEEDBACK`` event is a
statement *about* a finished run, so it is allowed through the system-observation path.
Everything else -- a tool result, a failure, a recovery -- is an agent mutation, and
mutation after the terminal status is refused exactly as it is for the in-process SDK
(section 22). A late tool result is not an exception to that rule: it means the
adapter's own ordering is wrong, and quietly appending it would rewrite history.

Recovery is the one place where the external path keeps state: ``recovery_started``
writes an open :class:`~aer.runtime.models.RecoveryRecord` and ``recovery_finished``
completes it. That mirrors :class:`~aer.runtime.hooks.RecoveryContext` for the same
reason -- a recovery trajectory is the highest-value distillation input, and an
interrupted process must leave a visible ``success IS NULL`` row rather than nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from aer.exceptions import AERError, RecordNotFoundError, RunStateError
from aer.runtime.enums import EventType
from aer.runtime.models import ErrorRecord, Event, RecoveryRecord
from aer.runtime.sanitization import (
    EXTERNAL_STACK_TRACE_MAX_LENGTH,
    MESSAGE_MAX_LENGTH,
    truncate,
)
from aer.runtime.serialization import JsonObject, to_json_object, to_json_value

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from aer.runtime.run import RunContext

__all__ = ["EXTERNAL_EVENT_TYPES", "ExternalEventRecorder"]

#: The event types an external source may record as a plain event.
#:
#: Small on purpose, and closed. ``TASK_START``/``TASK_END`` belong to the run's
#: lifecycle and are driven by opening and finishing a session; ``ERROR`` goes
#: through :meth:`ExternalEventRecorder.failure` so the error pipeline is not
#: bypassed; ``RECOVERY_*`` goes through the two recovery methods so a record exists
#: behind the events; and ``VERIFICATION`` is deliberately **absent** -- a verdict is
#: only ever written by the verification engine, so an adapter can relay evidence to a
#: real verifier but can never declare a pass itself (round-8 section 39).
EXTERNAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.MODEL_CALL,
        EventType.MODEL_RESULT,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.HUMAN_FEEDBACK,
    }
)


class ExternalEventRecorder:
    """The operations an outside observer may perform on one run.

    Bound to a single run and a single ``source`` at construction, because provenance
    is not optional: an event in the trace whose origin nobody recorded is an event
    nobody can interpret later (round-8 brief, section 38). The source string becomes
    ``metadata["source"]`` on everything this object writes, so "which integration put
    this here?" is answerable from the trace alone.
    """

    def __init__(self, run: RunContext, *, source: str) -> None:
        if not source:
            raise AERError("An external event source must be named: provenance is not optional")
        self._run = run
        self._source = source

    # -- introspection -----------------------------------------------------

    @property
    def run(self) -> RunContext:
        """The run these events are recorded against."""
        return self._run

    @property
    def source(self) -> str:
        """The integration this recorder speaks for."""
        return self._source

    def __repr__(self) -> str:
        return f"ExternalEventRecorder(run_id={self._run.run_id!r}, source={self._source!r})"

    # -- events ------------------------------------------------------------

    def event(
        self,
        event_type: EventType,
        *,
        input: object | None = None,
        output: object | None = None,
        duration_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Event:
        """Record one externally observed event.

        Args:
            event_type: Must be in :data:`EXTERNAL_EVENT_TYPES`.
            input / output: The event body, already normalised for JSON by the caller.
            duration_ms: The source's own measurement, when it has one.
            metadata: Extra JSON merged over the provenance this recorder adds.

        Raises:
            RunStateError: the event type is not one an external source may record, or
                the run already finished and the event is not an observation.
        """
        event_type = EventType(event_type)
        if event_type not in EXTERNAL_EVENT_TYPES:
            raise RunStateError(
                f"{event_type.value} cannot be recorded as an external event. Allowed: "
                f"{sorted(item.value for item in EXTERNAL_EVENT_TYPES)}. Lifecycle "
                "transitions go through start/finish, failures through failure(), "
                "recoveries through recovery_started()/recovery_finished(), and "
                "verdicts only through the verification engine."
            )

        meta = self._meta(metadata)
        if event_type is EventType.HUMAN_FEEDBACK:
            # A statement *about* a finished run, so it is a system observation and
            # may arrive after the agent stopped (round-8 section 22).
            return self._run._append_system_event(
                event_type,
                input=to_json_value(input),
                output=to_json_value(output),
                duration_ms=duration_ms,
                metadata=meta,
            )
        return self._run.emit(
            event_type,
            input=to_json_value(input),
            output=to_json_value(output),
            duration_ms=duration_ms,
            metadata=meta,
        )

    # -- failures ----------------------------------------------------------

    def failure(
        self,
        *,
        error_type: str,
        message: str,
        stack_trace: str | None = None,
        recoverable: bool = True,
        metadata: Mapping[str, object] | None = None,
    ) -> ErrorRecord:
        """Record a failure the source reported, through the one error pipeline.

        The message and the stack are truncated rather than stored whole: they come
        from outside AER and can be megabytes of terminal output (section 27). The
        truncation marker states how much was dropped, so nothing looks complete when
        it is not.

        Raises:
            RunStateError: the run already finished. A failure that arrives after the
                run ended would rewrite history, so it is refused rather than appended.
        """
        record = self._run._record_failure(
            error_type=truncate(error_type, MESSAGE_MAX_LENGTH),
            error_message=truncate(message, MESSAGE_MAX_LENGTH),
            stack_trace=(
                None
                if stack_trace is None
                else truncate(stack_trace, EXTERNAL_STACK_TRACE_MAX_LENGTH)
            ),
            recoverable=recoverable,
            metadata=self._meta(metadata),
            system=False,
        )
        return record

    # -- recovery ----------------------------------------------------------

    def recovery_started(
        self,
        reason: str,
        *,
        error_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RecoveryRecord:
        """Open a repair attempt: ``RECOVERY_START`` plus an open record.

        The record is persisted immediately, exactly as the in-process hook does, so
        an interrupted process leaves evidence that an attempt was under way.

        Raises:
            RecordNotFoundError: ``error_id`` names no known error.
            AERError: ``error_id`` belongs to a different run.
            RunStateError: the run already finished.
        """
        self._require_error_of_this_run(error_id, action="link a recovery to")
        meta = self._meta(metadata)

        opening = self._run.emit(
            EventType.RECOVERY_START,
            input=to_json_value({"reason": reason, "error_id": error_id}),
            metadata=meta,
        )
        return self._run.runtime.recoveries.create(
            RecoveryRecord(
                run_id=self._run.run_id,
                reason=reason,
                error_id=error_id,
                start_event_id=opening.id,
                metadata=meta,
            )
        )

    def recovery_finished(
        self,
        recovery_id: str,
        *,
        success: bool,
        outcome: object | None = None,
        duration_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RecoveryRecord:
        """Close a repair attempt: ``RECOVERY_RESULT`` plus the completed record.

        A successful attempt that named an error resolves exactly that error --
        nothing else ever does (round-3 brief, section 18).

        Raises:
            RecordNotFoundError: no such recovery exists.
            AERError: the recovery belongs to a different run.
            RunStateError: the recovery was already completed.
        """
        recovery = self._run.runtime.recoveries.get(recovery_id)
        if recovery is None:
            raise RecordNotFoundError(f"Cannot complete unknown recovery: {recovery_id}")
        if recovery.run_id != self._run.run_id:
            raise AERError(
                f"Recovery {recovery_id} belongs to run {recovery.run_id}, "
                f"not to run {self._run.run_id}"
            )
        if recovery.success is not None:
            raise RunStateError(
                f"Recovery {recovery_id} is already completed "
                f"(success={recovery.success}); a repair attempt ends once"
            )

        normalized = to_json_value(outcome)
        payload: dict[str, object] = {
            "reason": recovery.reason,
            "success": success,
            "error_id": recovery.error_id,
            "result": normalized,
        }
        meta = self._meta(metadata)

        result_event = self._run.emit(
            EventType.RECOVERY_RESULT,
            output=to_json_value(payload),
            duration_ms=duration_ms,
            metadata=meta,
        )

        recovery.success = success
        recovery.duration_ms = duration_ms
        recovery.ended_at = result_event.created_at
        recovery.result_event_id = result_event.id
        if outcome is not None:
            recovery.outcome = normalized
        updated = self._run.runtime.recoveries.update(recovery)

        if success and recovery.error_id is not None:
            self._run.runtime.errors.mark_resolved(recovery.error_id)
        return updated

    # -- internals ---------------------------------------------------------

    def _meta(self, metadata: Mapping[str, object] | None) -> JsonObject:
        """Caller metadata over the provenance block, so the source cannot be lost.

        The caller's keys come second: an adapter may add to the metadata, but it may
        not rename the source it is recording under.
        """
        return to_json_object({**{"source": self._source}, **(metadata or {})})

    def _require_error_of_this_run(self, error_id: str | None, *, action: str) -> None:
        if error_id is None:
            return
        existing = self._run.runtime.errors.get(error_id)
        if existing is None:
            raise RecordNotFoundError(f"Cannot {action} unknown error: {error_id}")
        if existing.run_id != self._run.run_id:
            raise AERError(
                f"Error {error_id} belongs to run {existing.run_id}, not to run {self._run.run_id}"
            )
