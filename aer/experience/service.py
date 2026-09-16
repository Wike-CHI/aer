"""``ExperienceService`` -- the application layer that turns a run into knowledge.

The whole pipeline lives in one place, in one order, so the guarantees are readable
rather than scattered (round-5 brief, section 26)::

    1. idempotency   a run already linked to an experience returns that experience
    2. evidence      build the immutable RunEvidence package
    3. policy        is this run worth a distillation pass at all?
    4. distill       ask the provider (crashes are recorded, nothing is written)
    5. validate      refuse a candidate with no identity
    6. fingerprint   normalise (kind, domain, title, problem)
    7. dedup         same claim -> attach the run, do not create a twin
    8. create        Experience at DISTILLED + its source, in one transaction
    9. verify        DISTILLED -> VERIFIED when the outcome is evidence-backed

Four invariants this ordering buys, each with a failure mode it prevents:

* **Nothing half-written.** Every step that can fail runs *before* the first write.
  A provider crash, an unusable candidate or a missing provider leaves the database
  exactly as it was (sections 43, 50).
* **Idempotent.** Step 1 short-circuits a re-run, and ``experience_sources`` refuses
  a duplicate link at the storage level, so the same run cannot inflate the store
  (sections 42, 52).
* **Kinds come from facts.** Step 3/6 use
  :func:`~aer.experience.classify.classify_kind`; a provider that claims success
  while a verifier disagreed produces a ``FAILURE`` experience with the disagreement
  recorded (sections 27, 54).
* **Provenance is never lost.** The source link is written in the same transaction as
  the experience, so knowledge cannot exist without a run that justifies it
  (section 13).

What this service deliberately does **not** do: retrieve, rank, inject, count reuse
or promote. Those are Milestones 6 and 7 (section 56).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aer.exceptions import DistillationError
from aer.experience.candidate import ExperienceCandidate, validate_candidate
from aer.experience.classify import outcome_is_verified
from aer.experience.dedup import dedup_key_for
from aer.experience.distiller import ExperienceDistiller
from aer.experience.evidence import RunEvidence, RunEvidenceBuilder
from aer.experience.policy import DEFAULT_POLICY, DistillationDecision, DistillationPolicy
from aer.experience.provider import DistillationProvider
from aer.runtime.enums import ExperienceKind, ExperienceStatus
from aer.runtime.models import Experience, ExperienceSource
from aer.runtime.run import RunContext
from aer.runtime.serialization import to_json_object

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from aer.runtime.runtime import AER
    from aer.storage.repositories import ExperienceRepository, ExperienceSourceRepository

#: Provider identity recorded when distillation was not configured.
UNCONFIGURED = "unconfigured"


class ExperienceService:
    """Distils runs into experiences, idempotently."""

    def __init__(
        self,
        *,
        runtime: AER,
        experiences: ExperienceRepository,
        sources: ExperienceSourceRepository,
        evidence: RunEvidenceBuilder,
        policy: DistillationPolicy | None = None,
        provider: DistillationProvider | None = None,
    ) -> None:
        self._runtime = runtime
        self._experiences = experiences
        self._sources = sources
        self._evidence = evidence
        self._policy = policy or DEFAULT_POLICY
        self._distiller = None if provider is None else ExperienceDistiller(provider)

    # -- introspection -----------------------------------------------------

    @property
    def policy(self) -> DistillationPolicy:
        """The trigger policy in force."""
        return self._policy

    @property
    def evidence(self) -> RunEvidenceBuilder:
        """The builder that assembles what a distillation pass may look at.

        Public because the evidence package is the honest answer to "what did AER
        know about this run when it learned from it?", and answering that should not
        require reaching into a private attribute.
        """
        return self._evidence

    @property
    def provider_name(self) -> str:
        """Provider identity, or ``"unconfigured"`` when none was supplied."""
        return UNCONFIGURED if self._distiller is None else self._distiller.provider_name

    @property
    def is_configured(self) -> bool:
        """Whether a distillation provider is available."""
        return self._distiller is not None

    def __repr__(self) -> str:
        return f"ExperienceService(policy={self._policy.name!r}, provider={self.provider_name!r})"

    # -- read-only probe ---------------------------------------------------

    def evaluate(
        self,
        run_id: str,
        *,
        explicit_high_value: bool = False,
    ) -> DistillationDecision:
        """Decide whether ``run_id`` would be distilled, without distilling it.

        The dry run a caller uses to answer "was this run interesting?" without
        paying for a provider call.

        Raises:
            RecordNotFoundError: no such run exists.
        """
        evidence = self._evidence.build(run_id)
        return self._policy.evaluate(evidence, explicit_high_value=explicit_high_value)

    # -- the pipeline ------------------------------------------------------

    def distill_run(
        self,
        run_id: str,
        *,
        explicit_high_value: bool = False,
    ) -> Experience | None:
        """Turn ``run_id`` into an experience, or explain why it did not.

        Args:
            run_id: The run to distil.
            explicit_high_value: Keep this run even if the policy finds nothing
                interesting about it (brief section 17).

        Returns:
            The experience this run belongs to -- newly created, or an existing one
            that this run now also supports -- or ``None`` when the policy declined.
            ``None`` is a normal outcome: most runs teach nothing.

        Raises:
            RecordNotFoundError: no such run exists.
            DistillationError: no provider is configured, or the provider returned
                something unusable. **Nothing is persisted** in that case.
            CandidateValidationError: the candidate had no identity.
            BaseException: whatever the provider raised, re-raised unchanged after
                being recorded as an ``ERROR`` system observation on the run.
        """
        # 1. Idempotency before anything expensive.
        already = self._existing_for_run(run_id)
        if already is not None:
            return already

        # 2. Evidence.
        evidence = self._evidence.build(run_id)

        # 3. Policy.
        decision = self._policy.evaluate(evidence, explicit_high_value=explicit_high_value)
        if not decision.should_distill or decision.kind is None:
            return None
        kind = decision.kind

        # 4. Distillation -- the only step that can run arbitrary code.
        candidate = self._distil(evidence)

        # 5. Structural validation, before any identity is derived from the text.
        validate_candidate(candidate)

        # 6. Fingerprint.
        dedup_key = dedup_key_for(
            kind=kind,
            domain=candidate.domain,
            title=candidate.title,
            problem=candidate.problem,
        )

        # 7. Duplicate of something already known? Attach, do not twin.
        twin = self._find_duplicate(dedup_key)
        if twin is not None:
            return self._attach_source(twin, evidence, kind)

        # 8. Persist at DISTILLED. The trajectory has genuinely been compressed by
        #    now, and the row is written together with its provenance.
        experience = self._build_experience(evidence, decision, candidate, dedup_key)
        stored = self._experiences.create(experience, source_run_id=evidence.run_id)

        # 9. The verdict about the outcome is a separate, fallible step.
        return self._mark_outcome(stored, kind, evidence)

    # -- steps -------------------------------------------------------------

    def _existing_for_run(self, run_id: str) -> Experience | None:
        """Return the experience this run already supports, if any.

        The idempotency probe (section 42). It deliberately also matches a
        *deprecated* experience: the run has been distilled, and producing a second
        experience for the same run would leave two claims sharing one piece of
        evidence.
        """
        linked = self._sources.get_experiences_for_run(run_id)
        if not linked:
            return None
        return self._experiences.get(linked[0])

    def _distil(self, evidence: RunEvidence) -> ExperienceCandidate:
        """Run the provider, recording a crash on the run's own trace.

        Raises:
            DistillationError: no provider is configured. Raised *before* the trace
                is touched: a missing provider is a deployment mistake that would
                otherwise repeat on every call and bury the run's real history.
            BaseException: whatever the provider raised, re-raised unchanged.
        """
        if self._distiller is None:
            raise DistillationError(
                f"No distillation provider is configured for run {evidence.run_id}; "
                "pass one to AER(distillation_provider=...)"
            )

        try:
            return self._distiller.distill(evidence)
        except BaseException as exc:
            self._report_crash(evidence, exc)
            raise

    def _find_duplicate(self, dedup_key: str) -> Experience | None:
        """An existing, non-deprecated experience with the same fingerprint.

        Deprecated experiences are not candidates for merging: withdrawal has to mean
        something, so a claim that was withdrawn is not silently revived by new
        evidence (D-034).
        """
        matches = self._experiences.find_by_dedup_key(dedup_key)
        return matches[0] if matches else None

    def _attach_source(
        self,
        experience: Experience,
        evidence: RunEvidence,
        kind: ExperienceKind,
    ) -> Experience:
        """Record that this run also supports ``experience``.

        The claim itself is never rewritten by an arriving run: the title, problem,
        solution and fingerprint stay as first distilled. ``updated_at`` and
        ``confidence`` stay put too -- an extra supporting run is not a calibrated
        quality signal yet (D-037), and the new source row already records the
        reinforcement.

        One thing *can* change: an experience sitting at ``DISTILLED`` is promoted to
        ``VERIFIED`` when the arriving run's outcome is itself evidence-backed.
        Without this, a claim that was first recorded from an unverified run would
        stay permanently unverified even after a run that a verifier confirmed
        supported it -- the store would be discarding its own strongest evidence.
        Promotion is the only move allowed here; nothing ever demotes a claim, and a
        withdrawn (``DEPRECATED``) experience is not reachable at all, because
        :meth:`_find_duplicate` excludes it.

        The source link is written first: provenance is the invariant that must
        never be lost, and the promotion is an improvement on top of it.
        """
        self._sources.add(ExperienceSource(experience_id=experience.id, run_id=evidence.run_id))

        if experience.status is ExperienceStatus.DISTILLED and outcome_is_verified(kind, evidence):
            promoted = experience.mark_verified(
                reason=f"run {evidence.run_id} confirmed the outcome"
            )
            return self._experiences.update(promoted)

        return experience

    def _build_experience(
        self,
        evidence: RunEvidence,
        decision: DistillationDecision,
        candidate: ExperienceCandidate,
        dedup_key: str,
    ) -> Experience:
        """Assemble the experience to store, at ``DISTILLED``.

        Three provider-supplied fields are handled with care rather than copied:

        * a ``kind`` that disagrees with the classification is *recorded* and ignored
          -- the disagreement is evidence about the provider (section 54);
        * on a ``FAILURE``, ``solution`` and ``recommended_workflow`` are moved into
          metadata and their fields are left empty. A failed run has, by definition,
          no confirmed fix, and leaving an unverified guess in ``solution`` would let
          a later retrieval present it as the answer -- the exact hazard sections 4
          and 33 warn about. Nothing is dropped: both are kept verbatim as
          hypotheses, so a future stage can surface them *as* hypotheses;
        * ``root_cause`` is kept even for a failure. It is a hypothesis either way
          (D-033), and it is the thing failure analysis needs most, so there is
          nothing to protect the reader from beyond the field's own documentation.
        """
        assert decision.kind is not None  # guaranteed by the caller
        kind = decision.kind

        metadata = to_json_object(
            {
                **candidate.metadata,
                "distillation": {
                    "provider": self.provider_name,
                    "policy": self._policy.name,
                    "triggers": [trigger.value for trigger in decision.triggers],
                    "reasons": list(decision.reasons),
                },
                # Kept as a hint for the milestone that can calibrate it, never as
                # the confidence itself (section 38).
                "confidence_hint": candidate.confidence_hint,
                "root_cause_confidence": candidate.root_cause_confidence,
            }
        )

        if candidate.kind is not None and candidate.kind is not kind:
            metadata["provider_suggested_kind"] = candidate.kind.value

        solution = candidate.solution
        recommended_workflow = candidate.recommended_workflow
        if kind is ExperienceKind.FAILURE:
            if solution is not None:
                metadata["provider_solution_hypothesis"] = solution
                solution = None
            if recommended_workflow:
                metadata["provider_recommended_workflow"] = list(recommended_workflow)
                recommended_workflow = ()

        failed_attempts = candidate.failed_attempts
        if not failed_attempts:
            # The provider said nothing about what was tried and did not work. The
            # run *did* record it, so report the observed failures rather than
            # leaving the most valuable field of a failure or recovery record empty
            # (sections 4 and 29). This is reporting recorded evidence, not
            # inventing an interpretation -- and when the run recorded no errors the
            # fallback is simply empty.
            failed_attempts = evidence.error_messages

        return Experience(
            kind=kind,
            domain=candidate.domain,
            title=candidate.title,
            problem=candidate.problem,
            dedup_key=dedup_key,
            symptoms=candidate.symptoms,
            failed_attempts=failed_attempts,
            root_cause=candidate.root_cause,
            solution=solution,
            recommended_workflow=recommended_workflow,
            avoid=candidate.avoid,
            status=ExperienceStatus.DISTILLED,
            # Always 0.0 for now: a real value needs reuse data (D-037).
            confidence=0.0,
            generalizable=candidate.generalizable,
            outcome_verified=False,
            metadata=metadata,
        )

    def _mark_outcome(
        self,
        experience: Experience,
        kind: ExperienceKind,
        evidence: RunEvidence,
    ) -> Experience:
        """Promote ``DISTILLED -> VERIFIED`` when the outcome is evidence-backed.

        A separate step because it is a separate fact, and one that can legitimately
        fail to hold: an agent that gave up with nothing checking the environment
        leaves an experience at ``DISTILLED`` with ``outcome_verified=False`` -- "we
        know this was abandoned, nobody confirmed the task was impossible".
        """
        if not outcome_is_verified(kind, evidence):
            return experience

        if kind is ExperienceKind.FAILURE:
            reason = (
                f"{evidence.required_failed} required verification(s) failed; "
                "the goal is confirmed unmet"
            )
        else:
            reason = (
                f"{evidence.verification_summary.required_passed} required "
                "verification(s) passed; the outcome is confirmed"
            )
        return self._experiences.update(experience.mark_verified(reason=reason))

    def _report_crash(self, evidence: RunEvidence, exc: BaseException) -> None:
        """Record a provider crash as a system observation on the run.

        Reuses the run's single error pipeline through a fresh ``RunContext``, the
        same way the verification engine does, so the trace shows *why* a run that
        was already finished suddenly grew an ``ERROR`` -- with
        ``metadata.source = "experience_distiller"`` to say it came from
        post-processing rather than from the agent (sections 43-44).

        The exception is never swallowed: the caller still sees it, and if the trace
        cannot be written the failure is attached with ``add_note`` instead of
        replacing the original.
        """
        try:
            context = RunContext(self._runtime, evidence.run)
            context._record_error(
                exc,
                recoverable=False,
                system=True,
                metadata={
                    "source": "experience_distiller",
                    "provider": self.provider_name,
                    "policy": self._policy.name,
                },
            )
        except Exception as recording_error:
            exc.add_note(
                "AER could not record the distillation failure on run "
                f"{evidence.run_id}: {recording_error!r}"
            )
