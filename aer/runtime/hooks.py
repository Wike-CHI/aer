"""Hook context managers: ``run.tool(...)`` and ``run.recovery(...)``.

Hooks **observe, record, link and measure** -- nothing more (round-3 brief,
section 35). No domain knowledge lives here: nothing in this module knows what
WordPress is or why a 403 happened. Business meaning belongs to the agent and,
later, to verifiers.

Both contexts share one lifecycle::

    __enter__  -> record the opening event, start a monotonic stopwatch
    __exit__   -> on exception: record ERROR through the run's single error
                  pipeline, record the closing event with success=false,
                  then let the exception propagate
                  on success: record the closing event with success=true

Invariants enforced here:

* the closing event is written on **every** exit path, including exceptions --
  a failed tool call never leaves an unfinished trace (section 8);
* exceptions are recorded but never swallowed; ``__exit__`` returns ``False``
  (section 9);
* durations come from :func:`time.perf_counter` (monotonic), never from
  wall-clock datetime arithmetic -- the system clock may move (section 7);
* every event goes through :meth:`~aer.runtime.run.RunContext.emit`, so hooks
  cannot bypass the single sequence-allocation path (section 25).
"""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import TYPE_CHECKING, Literal, Self

from aer.exceptions import AERError, HookStateError, RecordNotFoundError, RunStateError
from aer.runtime.enums import EventType
from aer.runtime.models import ErrorRecord, Event, RecoveryRecord
from aer.runtime.serialization import JsonValue, to_json_object, to_json_value

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from aer.runtime.run import RunContext


def _elapsed_ms(started_at: float) -> int:
    """Whole milliseconds since a :func:`time.perf_counter` reading."""
    return int((perf_counter() - started_at) * 1000)


class _RunHook:
    """Shared lifecycle for hook context managers.

    Deliberately not a public abstraction: it holds only the behaviour that must
    be *identical* in every hook -- the state guards, the monotonic stopwatch and
    the record-but-never-swallow exit policy. Subclasses supply the payloads.
    """

    def __init__(
        self,
        run: RunContext,
        *,
        hook_name: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._run = run
        self._hook_name = hook_name
        self._metadata = to_json_object(metadata)
        self._started_at: float | None = None
        self._closed = False
        self._opening_event: Event | None = None
        self._error_record: ErrorRecord | None = None

    # -- introspection -----------------------------------------------------

    @property
    def run(self) -> RunContext:
        """The run this hook records into."""
        return self._run

    @property
    def hook_name(self) -> str:
        """``"tool"`` or ``"recovery"``; used in error messages and metadata."""
        return self._hook_name

    @property
    def opening_event(self) -> Event | None:
        """The event recorded on entry, or ``None`` before ``__enter__``."""
        return self._opening_event

    @property
    def error_record(self) -> ErrorRecord | None:
        """The error this hook recorded, or ``None`` when it did not fail.

        Exposed so a caller can link a subsequent recovery to the failure without
        digging through event payloads::

            tool = run.tool("wordpress.update_page")
            ...
            with run.recovery(reason="403", error_id=tool.error_record.id):
                ...
        """
        return self._error_record

    @property
    def is_closed(self) -> bool:
        """``True`` once ``__exit__`` has run."""
        return self._closed

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Self:
        """Open the hook: guard the state, start the clock, record the start event.

        Raises:
            RunStateError: the run already reached a terminal status.
            HookStateError: this context object was already opened or closed.
        """
        if self._run.is_finished:
            raise RunStateError(
                f"Run {self._run.run_id} is already finished "
                f"({self._run.status.value}); cannot open a {self._hook_name} hook"
            )
        if self._closed:
            raise HookStateError(f"This {self._hook_name} context has already been used")
        if self._started_at is not None:
            raise HookStateError(f"This {self._hook_name} context is already open")

        self._started_at = perf_counter()
        self._opening_event = self._enter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        """Close the hook, always writing the closing event. Never suppresses."""
        # `exc_type` and `traceback` are unused: the exception object itself carries
        # everything the hook has to record.
        if self._started_at is None:
            raise HookStateError(f"This {self._hook_name} context was never opened")

        duration_ms = _elapsed_ms(self._started_at)
        self._closed = True
        failure = exc if isinstance(exc, BaseException) else None

        try:
            self._finish(failure, duration_ms)
        except Exception as recording_error:
            if failure is None:
                # Nothing to protect: the recording failure is the real failure.
                raise
            # The caller expects *its* exception to propagate (section 9), so the
            # recording failure is attached to it rather than replacing it. It is
            # still surfaced -- just not at the cost of the original cause.
            failure.add_note(
                f"AER could not record this {self._hook_name} hook: {recording_error!r}"
            )

        return False

    # -- extension points --------------------------------------------------

    def _enter(self) -> Event:
        """Record and return the opening event."""
        raise NotImplementedError

    def _finish(self, error: BaseException | None, duration_ms: int) -> None:
        """Record the closing event, and whatever record the hook owns."""
        raise NotImplementedError

    def _guard_open(self) -> None:
        """Reject ``set_result``-style calls made outside an open context."""
        if self._closed:
            raise HookStateError(f"This {self._hook_name} context is already closed")
        if self._started_at is None:
            raise HookStateError(f"This {self._hook_name} context is not open")

    def _record_failure(self, error: BaseException) -> ErrorRecord:
        """Record a hook failure through the run's single error pipeline.

        Every hook goes through here, so there is exactly one place where a hook
        failure becomes an ``ERROR`` event plus an ``ErrorRecord`` (section 14).
        """
        record = self._run.error(error, metadata=self._metadata)
        self._error_record = record
        return record


class ToolContext(_RunHook):
    """Records one tool invocation as ``TOOL_CALL`` -> [``ERROR``] -> ``TOOL_RESULT``.

    Usage::

        with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
            tool.set_result(update_page())

    ``set_result`` is the only way to publish a return value. AER deliberately
    does not inspect the ``with`` block's locals or guess a return value
    (round-3 brief, section 6): if the block exits normally without calling
    ``set_result``, ``TOOL_RESULT`` is still recorded and its ``result`` is null.
    """

    def __init__(
        self,
        run: RunContext,
        name: str,
        *,
        input: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            run,
            hook_name="tool",
            metadata={**(metadata or {}), "hook": "tool", "tool": name},
        )
        self._name = name
        self._input = input
        self._result: object | None = None
        self._has_result = False
        self._result_event: Event | None = None

    # -- introspection -----------------------------------------------------

    @property
    def name(self) -> str:
        """The tool identifier, e.g. ``"wordpress.update_page"``."""
        return self._name

    @property
    def call_event(self) -> Event | None:
        """The recorded ``TOOL_CALL`` event."""
        return self._opening_event

    @property
    def result_event(self) -> Event | None:
        """The recorded ``TOOL_RESULT`` event."""
        return self._result_event

    @property
    def result(self) -> JsonValue | None:
        """The value passed to :meth:`set_result`, already normalised for JSON."""
        return to_json_value(self._result) if self._has_result else None

    # -- public API --------------------------------------------------------

    def set_result(self, result: object) -> None:
        """Publish the tool's outcome.

        Calling it twice keeps the last value: retrying inside one tool block is a
        legitimate pattern, and failing on the second call would punish it.
        """
        self._guard_open()
        self._result = result
        self._has_result = True

    # -- lifecycle ---------------------------------------------------------

    def _enter(self) -> Event:
        return self._run.emit(
            EventType.TOOL_CALL,
            input={"tool": self._name, "arguments": self._input},
            metadata=self._metadata,
        )

    def _finish(self, error: BaseException | None, duration_ms: int) -> None:
        payload: dict[str, object] = {"tool": self._name, "success": error is None}

        if error is None:
            payload["result"] = self.result
        else:
            # Same error pipeline as `run.error(...)` -- never a second one
            # (round-3 brief, section 14). ERROR is written before TOOL_RESULT so
            # the trace reads TOOL_CALL -> ERROR -> TOOL_RESULT.
            record = self._record_failure(error)
            payload["error"] = {
                "error_id": record.id,
                "error_type": record.error_type,
                "error_message": record.error_message,
            }

        self._result_event = self._run.emit(
            EventType.TOOL_RESULT,
            output=to_json_value(payload),
            duration_ms=duration_ms,
            metadata=self._metadata,
        )


class RecoveryContext(_RunHook):
    """Records a repair attempt as ``RECOVERY_START`` -> [``ERROR``] -> ``RECOVERY_RESULT``.

    Usage::

        try:
            with run.tool("wordpress.update_page") as tool:
                tool.set_result(update_page())
        except PermissionError as exc:
            with run.recovery(reason="REST API 403", error_id=error.id):
                fix_permissions()

    A successful attempt that named an ``error_id`` marks exactly that error
    resolved. Nothing else does -- a later successful task never retroactively
    resolves history (round-3 brief, section 18).
    """

    def __init__(
        self,
        run: RunContext,
        reason: str,
        *,
        error_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            run,
            hook_name="recovery",
            metadata={**(metadata or {}), "hook": "recovery"},
        )
        self._reason = reason
        self._error_id = error_id
        self._outcome: object | None = None
        self._has_outcome = False
        self._record: RecoveryRecord | None = None
        self._result_event: Event | None = None

    # -- introspection -----------------------------------------------------

    @property
    def reason(self) -> str:
        """Why the agent decided to recover."""
        return self._reason

    @property
    def error_id(self) -> str | None:
        """The error this attempt targets, if the caller named one."""
        return self._error_id

    @property
    def record(self) -> RecoveryRecord | None:
        """The persisted recovery record (created on entry, completed on exit)."""
        return self._record

    @property
    def result_event(self) -> Event | None:
        """The recorded ``RECOVERY_RESULT`` event."""
        return self._result_event

    @property
    def succeeded(self) -> bool | None:
        """``None`` while open, then ``True``/``False``."""
        return None if self._record is None else self._record.success

    # -- public API --------------------------------------------------------

    def set_result(self, outcome: object) -> None:
        """Record what the repair attempt produced.

        Optional: the brief asks for ``reason``/``success``/``duration_ms``, so an
        attempt that produces no value is perfectly valid.
        """
        self._guard_open()
        self._outcome = outcome
        self._has_outcome = True

    # -- lifecycle ---------------------------------------------------------

    def _enter(self) -> Event:
        self._verify_linked_error()

        opening = self._run.emit(
            EventType.RECOVERY_START,
            input={"reason": self._reason, "error_id": self._error_id},
            metadata=self._metadata,
        )

        # Persisted immediately so an interrupted process leaves a visible row with
        # success IS NULL rather than no row at all.
        self._record = self._run.runtime.recoveries.create(
            RecoveryRecord(
                run_id=self._run.run_id,
                reason=self._reason,
                error_id=self._error_id,
                start_event_id=opening.id,
                metadata=self._metadata,
            )
        )
        return opening

    def _finish(self, error: BaseException | None, duration_ms: int) -> None:
        payload: dict[str, object] = {
            "reason": self._reason,
            "success": error is None,
            "error_id": self._error_id,
        }

        if error is None:
            payload["result"] = to_json_value(self._outcome) if self._has_outcome else None
        else:
            record = self._record_failure(error)
            payload["error"] = {
                "error_id": record.id,
                "error_type": record.error_type,
                "error_message": record.error_message,
            }

        self._result_event = self._run.emit(
            EventType.RECOVERY_RESULT,
            output=to_json_value(payload),
            duration_ms=duration_ms,
            metadata=self._metadata,
        )

        recovery = self._record
        if recovery is None:  # pragma: no cover - _enter always sets it
            raise AERError("Recovery record was not created on entry")

        recovery.success = error is None
        recovery.duration_ms = duration_ms
        recovery.ended_at = self._result_event.created_at
        recovery.result_event_id = self._result_event.id
        if self._has_outcome:
            recovery.outcome = to_json_value(self._outcome)
        self._record = self._run.runtime.recoveries.update(recovery)

        # Explicit, targeted resolution -- and only after the attempt succeeded.
        if error is None and self._error_id is not None:
            self._run.runtime.errors.mark_resolved(self._error_id)

    def _verify_linked_error(self) -> None:
        """Fail fast when ``error_id`` does not name an error of this run.

        Checked on entry, before any event is written, so a bad link cannot leave a
        dangling ``RECOVERY_START`` behind.

        Raises:
            RecordNotFoundError: no such error exists.
            AERError: the error belongs to a different run.
        """
        if self._error_id is None:
            return

        existing = self._run.runtime.errors.get(self._error_id)
        if existing is None:
            raise RecordNotFoundError(f"Cannot link recovery to unknown error: {self._error_id}")
        if existing.run_id != self._run.run_id:
            raise AERError(
                f"Error {self._error_id} belongs to run {existing.run_id}, "
                f"not to run {self._run.run_id}"
            )
