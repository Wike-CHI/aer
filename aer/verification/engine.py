"""The single verification pipeline: run a verifier, record what happened.

Every verdict in AER is produced here. ``RunContext.verify`` and ``AER.verify`` do
not contain a second implementation -- they resolve a run and delegate -- so there
is exactly one place where verdict shape, event payload, sanitisation and storage
order are decided (round-4 brief, section 11).

The order inside :meth:`VerificationEngine.verify` is not incidental:

1. the ``VERIFICATION`` event is written **first**, carrying the verdict summary
   and the ``verification_id`` that is about to exist. A timeline reader who never
   opens the ``verifications`` table still learns what happened, and an interrupted
   process leaves an event rather than nothing;
2. the ``VerificationRecord`` is written second, pointing back at that event.

No verifier code runs inside a database session: a verifier may take seconds and
may raise, and holding a write transaction open across it would couple AER's
storage health to a third party's latency.

A crash is not a verdict. If the verifier itself raises, the engine records an
``ERROR`` event through the run's one error pipeline and re-raises -- it never
invents ``passed=False``, because "the check could not be performed" and "the check
was performed and failed" are different facts about the world. A crashed verifier
leaves no ``VerificationRecord`` at all (round-4 brief, sections 14 and 40).
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING

from aer.exceptions import AERError, RecordNotFoundError, VerificationError
from aer.runtime.enums import EventType
from aer.runtime.models import VerificationRecord
from aer.runtime.sanitization import MESSAGE_MAX_LENGTH, redact, truncate
from aer.runtime.serialization import JsonObject, new_id, to_json_object, to_json_value, utc_now
from aer.verification.base import VerificationContext, VerificationResult, Verifier
from aer.verification.summary import VerificationSummary, is_verified_success

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from collections.abc import Mapping

    from aer.runtime.run import RunContext
    from aer.storage.repositories import RunRepository, VerificationRepository


class VerificationEngine:
    """Runs verifiers and turns their answers into durable, queryable records."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        verifications: VerificationRepository,
    ) -> None:
        self._runs = runs
        self._verifications = verifications

    # -- the pipeline ------------------------------------------------------

    def verify(
        self,
        run: RunContext,
        verifier: Verifier,
        *,
        context: VerificationContext | None = None,
        required: bool | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> VerificationRecord:
        """Execute ``verifier`` against ``run`` and record the verdict.

        Args:
            run: The run being verified. May already be terminal: verification is a
                system observation, not an agent mutation (see ``docs/DECISIONS.md``).
            verifier: Any object satisfying the :class:`Verifier` protocol.
            context: The evidence to check. Defaults to a context carrying only the
                run id and task, in which case a verifier that needs evidence will
                raise rather than guess.
            required: Override the verifier's own ``required`` flag for this verdict.
            metadata: Extra JSON metadata for the event and the record.

        Returns:
            The persisted verdict.

        Raises:
            VerificationError: the object passed is not a verifier, or it returned
                something that is not a verdict.
            VerificationInputError: the verifier could not obtain its evidence.
            AERError: ``context`` belongs to a different run.
            BaseException: any exception the verifier itself raised, re-raised
                unchanged after being recorded as an ``ERROR`` event.
        """
        if not isinstance(verifier, Verifier):
            raise VerificationError(
                f"{verifier!r} does not implement the Verifier protocol "
                "(needs name, verifier_type, required and verify())"
            )

        verification_context = self._context_for(run, context)
        is_required = verifier.required if required is None else required
        meta = to_json_object(metadata)

        started_at = perf_counter()
        try:
            result = verifier.verify(verification_context)
            if not isinstance(result, VerificationResult):
                # Guarded here rather than in the protocol: a third-party verifier
                # can return anything, and a malformed verdict must not reach
                # storage.
                raise VerificationError(
                    f"Verifier {verifier.name!r} returned {type(result).__name__}; "
                    "a verifier must return a VerificationResult"
                )
        except BaseException as exc:
            self._report_crash(run, verifier, exc, meta)
            raise
        duration_ms = int((perf_counter() - started_at) * 1000)

        # The id is minted before the event so the event can reference the verdict
        # it announces.
        verification_id = new_id()
        message = None if result.message is None else self._sanitise_message(result.message)

        event = run._append_system_event(
            EventType.VERIFICATION,
            output=to_json_value(
                {
                    "verification_id": verification_id,
                    "verifier_name": verifier.name,
                    "verifier_type": verifier.verifier_type.value,
                    "required": is_required,
                    "passed": result.passed,
                    "score": result.score,
                    "message": message,
                }
            ),
            duration_ms=duration_ms,
            metadata=meta,
        )

        return self._verifications.create(
            VerificationRecord(
                id=verification_id,
                run_id=run.run_id,
                event_id=event.id,
                verifier_type=verifier.verifier_type,
                verifier_name=verifier.name,
                passed=result.passed,
                required=is_required,
                score=result.score,
                message=message,
                result=result.result,
                created_at=utc_now(),
                metadata={**result.metadata, **meta},
            )
        )

    # -- aggregation -------------------------------------------------------

    def summarize(self, run_id: str) -> VerificationSummary:
        """Aggregate every verdict recorded for ``run_id``.

        Raises:
            RecordNotFoundError: no such run exists. An unknown run id is far more
                likely to be a typo than a run with no verdicts, and silently
                returning "0 verdicts, nothing passed" would hide it.
        """
        if self._runs.get(run_id) is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        return self._aggregate(run_id)

    def verified_success(self, run_id: str) -> bool:
        """Whether ``run_id`` is an agent success *and* independently confirmed.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        return is_verified_success(run.status, self._aggregate(run_id))

    def _aggregate(self, run_id: str) -> VerificationSummary:
        """Build the summary for a run that is already known to exist."""
        return VerificationSummary.from_records(run_id, self._verifications.get_by_run(run_id))

    # -- internals ---------------------------------------------------------

    def _context_for(
        self, run: RunContext, context: VerificationContext | None
    ) -> VerificationContext:
        """Resolve the context to verify with, rejecting a mismatched run id.

        Checked before anything is written, so a copy-pasted context cannot attach
        a verdict to the wrong run.
        """
        if context is None:
            return VerificationContext(run_id=run.run_id, task=run.run.task_description)
        if context.run_id != run.run_id:
            raise AERError(
                f"VerificationContext belongs to run {context.run_id}, not to run {run.run_id}"
            )
        return context

    def _report_crash(
        self,
        run: RunContext,
        verifier: Verifier,
        exc: BaseException,
        metadata: JsonObject,
    ) -> None:
        """Record a verifier crash through the run's single error pipeline.

        Marked ``recoverable=False``: the agent cannot repair AER's verifier, so
        advertising this failure as recoverable would invite a pointless recovery
        loop. The exception itself is never swallowed -- the caller still sees it.
        """
        try:
            run._record_error(
                exc,
                recoverable=False,
                system=True,
                metadata={
                    **metadata,
                    "source": "verifier",
                    "verifier": verifier.name,
                    "verifier_type": verifier.verifier_type.value,
                },
            )
        except Exception as recording_error:
            # Same policy as the hooks (docs/DECISIONS.md D-019): if AER cannot
            # record the crash, say so on the original exception rather than
            # replacing it with a storage error.
            exc.add_note(
                f"AER could not record the crash of verifier {verifier.name!r}: {recording_error!r}"
            )

    @staticmethod
    def _sanitise_message(message: str) -> str:
        """Redact and cap a verifier's explanation before it reaches storage.

        ``message`` is free-form text chosen by arbitrary verifier code, so it gets
        the same treatment as an exception message (``docs/DECISIONS.md`` D-020):
        credential shapes removed, length capped. Structured ``result`` payloads
        are left alone, for the same reason as in Milestone 3 -- redacting them
        wholesale would corrupt legitimate evidence.
        """
        return truncate(redact(message), MESSAGE_MAX_LENGTH)
