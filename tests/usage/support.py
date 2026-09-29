"""Builders for the Milestone 7 tests.

Two kinds of helper live here, and the split is the same one the milestone itself
makes:

* builders that create **facts** -- runs that ended a particular way, and
  experiences that they support. These go through the real runtime API, because the
  effectiveness report reads the real ``runs`` and ``verifications`` tables and a
  hand-forged row would prove nothing;
* builders that create **retrievals** -- scripting the index double and calling the
  tracked API, including the injection step, which every realistic scenario needs and
  which is otherwise four lines of bookkeeping per test.

Counts in these tests are deliberately small and explicit rather than generated: the
whole subject matter is which of several similar-looking states a row is in, and a
loop that produced twelve rows would make the assertions unreadable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aer import (
    AER,
    Experience,
    ExperienceKind,
    ExperienceStatus,
    ExperienceUsage,
    HttpStatusVerifier,
    RetrievalMode,
    RunStatus,
    TrackedRetrievalResult,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
    VerificationContext,
)
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.usage.fingerprints import context_fingerprint
from tests.knowledge.support import RecordingIndex, indexed_match

DEFAULT_QUERY = "WordPress REST API 403"
DEFAULT_DOMAIN = "wordpress"


# ---------------------------------------------------------------------------
# facts: experiences and runs
# ---------------------------------------------------------------------------


def store_experience(
    runtime: AER,
    *,
    experience_id: str = "exp-1",
    kind: ExperienceKind = ExperienceKind.RECOVERY,
    status: ExperienceStatus = ExperienceStatus.VERIFIED,
    domain: str = DEFAULT_DOMAIN,
    title: str = "WordPress REST API 403",
    problem: str = "Updating a page returns 403",
    solution: str | None = "use a credential with edit_posts",
    outcome_verified: bool = True,
    source_run_id: str | None = None,
) -> Experience:
    """Write one experience straight through the repository.

    Bypasses distillation on purpose: these tests are about usage tracking, and
    driving a whole distill pipeline to obtain one row would make every failure
    ambiguous between the two stages.
    """
    experience = Experience(
        id=experience_id,
        kind=kind,
        domain=domain,
        title=title,
        problem=problem,
        dedup_key=f"{kind.value}|{domain}|{title}|{problem}",
        solution=solution,
        status=status,
        outcome_verified=outcome_verified,
    )
    return runtime.experiences.create(experience, source_run_id=source_run_id)


def start_run(runtime: AER, *, run_id: str | None = None, task: str = "a later task") -> str:
    """Start a run and leave it running."""
    return runtime.start_run(task=task, task_type=DEFAULT_DOMAIN, run_id=run_id).run_id


def verified_success_run(runtime: AER, *, run_id: str | None = None) -> str:
    """A run the agent completed **and** an independent check confirmed."""
    context = runtime.start_run(task="a task that worked", task_type=DEFAULT_DOMAIN, run_id=run_id)
    context.success()
    context.verify(
        HttpStatusVerifier(expected_status=200, required=True),
        context=VerificationContext(run_id=context.run_id, payload={"actual_status": 200}),
    )
    return context.run_id


def verified_failure_run(runtime: AER, *, run_id: str | None = None) -> str:
    """A run whose failure an independent check confirmed.

    The agent declares success and the verifier disagrees -- the trajectory the whole
    runtime exists to distinguish from success (and the one an experience must never
    be credited for).
    """
    context = runtime.start_run(task="a task that failed", task_type=DEFAULT_DOMAIN, run_id=run_id)
    context.success()
    context.verify(
        HttpStatusVerifier(expected_status=200, required=True),
        context=VerificationContext(run_id=context.run_id, payload={"actual_status": 403}),
    )
    return context.run_id


def inconclusive_run(runtime: AER, *, run_id: str | None = None, verified: bool = False) -> str:
    """A run that ended without anyone declaring an outcome.

    The shape a CLI integration produces when its session hooks carry no result. With
    ``verified=True`` an independent check settled it instead, which is the only thing
    that can (round-8.1.1, D-100).
    """
    context = runtime.start_run(
        task="a session that ended without an outcome",
        task_type=DEFAULT_DOMAIN,
        run_id=run_id,
    )
    if verified:
        context.verify(
            HttpStatusVerifier(expected_status=200, required=True),
            context=VerificationContext(run_id=context.run_id, payload={"actual_status": 200}),
        )
    context.finish(RunStatus.INCONCLUSIVE)
    return context.run_id


def agent_failed_run(runtime: AER, *, run_id: str | None = None) -> str:
    """A run the agent itself declared failed, with nothing checking it."""
    context = runtime.start_run(task="a task given up on", task_type=DEFAULT_DOMAIN, run_id=run_id)
    context.fail()
    return context.run_id


def unverified_success_run(runtime: AER, *, run_id: str | None = None) -> str:
    """A run the agent declared successful that nobody verified."""
    context = runtime.start_run(
        task="a task nobody checked", task_type=DEFAULT_DOMAIN, run_id=run_id
    )
    context.success()
    return context.run_id


# ---------------------------------------------------------------------------
# retrievals
# ---------------------------------------------------------------------------


def match(
    experience_id: str = "exp-1",
    *,
    kind: str = "RECOVERY",
    status: str = "VERIFIED",
    outcome_verified: bool = True,
    **overrides: object,
):
    """One index hit with the trust evidence a real projection would carry."""
    return indexed_match(
        experience_id=experience_id,
        kind=kind,
        status=status,
        outcome_verified=outcome_verified,
        **overrides,  # type: ignore[arg-type]
    )


def indexed_for(
    runtime: AER,
    *,
    experience_id: str = "exp-1",
    kind: ExperienceKind = ExperienceKind.RECOVERY,
    status: ExperienceStatus = ExperienceStatus.VERIFIED,
    outcome_verified: bool = True,
    source_run_id: str | None = None,
) -> object:
    """Store an experience **and** return the index hit that would find it.

    Both halves together, because a hit for an experience SQLite no longer has is a
    foreign-key violation rather than a scenario: usage rows point at the store's row
    on purpose, so a test that wants that drift has to build it deliberately instead
    of getting it by accident.
    """
    store_experience(
        runtime,
        experience_id=experience_id,
        kind=kind,
        status=status,
        outcome_verified=outcome_verified,
        source_run_id=source_run_id,
    )
    return match(
        experience_id,
        kind=kind.value,
        status=status.value,
        outcome_verified=outcome_verified,
    )


def script_retrieval(
    index: RecordingIndex,
    *,
    guidance: Iterable[object] = (),
    failures: Iterable[object] = (),
    observations: Iterable[object] = (),
) -> None:
    """Queue the answers the index double will give, in the order it is asked.

    The retriever issues one search per class of record it may return -- guidance,
    then failures, then (in the wider modes) two observation classes, split by
    whether the status is below ``VERIFIED`` or at it with an unconfirmed outcome.
    ``observations`` fills the first of those two; the second is left empty, because a
    record in it would be one whose status claims verification while its outcome was
    never confirmed, which is not a state any test here needs. Anything left
    unscripted falls back to "no results".
    """
    index.script_results([list(guidance), list(failures), list(observations), []])


def retrieve(
    runtime: AER,
    index: RecordingIndex,
    *,
    guidance: Iterable[object] = (),
    failures: Iterable[object] = (),
    observations: Iterable[object] = (),
    query: str = DEFAULT_QUERY,
    run_id: str | None = None,
    domain: str | None = DEFAULT_DOMAIN,
    mode: RetrievalMode = RetrievalMode.GUIDANCE,
    limit: int = 3,
) -> TrackedRetrievalResult:
    """Script the index and perform one *tracked* retrieval."""
    script_retrieval(index, guidance=guidance, failures=failures, observations=observations)
    return runtime.retrieve_for_run(query, run_id=run_id, domain=domain, mode=mode, limit=limit)


def inject(
    runtime: AER,
    tracked: TrackedRetrievalResult,
    *,
    experience_ids: Sequence[str] | None = None,
    formatter: ExperienceContextFormatter | None = None,
    formatter_version: str = FORMATTER_VERSION,
) -> tuple[ExperienceUsage, ...]:
    """Record the injection of a tracked result, with realistic positions and sizes.

    The character counts come from the renderer rather than from a guess, which is
    also the recipe the public API documents: a caller that divides the total by the
    number of hits is recording an opinion, not a measurement.
    """
    renderer = formatter or ExperienceContextFormatter()
    ordered = list(tracked.result.all_hits)
    ranked = {hit.experience_id: rank for rank, hit in enumerate(ordered, start=1)}
    wanted = (
        set(experience_ids)
        if experience_ids is not None
        else {hit.experience_id for hit in ordered}
    )
    selected = [hit for hit in ordered if hit.experience_id in wanted]
    return runtime.record_injection(
        session_id=tracked.session_id,
        experience_ids=[hit.experience_id for hit in selected],
        context_fingerprint=context_fingerprint(renderer.format(tracked.result)),
        formatter_version=formatter_version,
        # Position is the hit's rank in the whole result, not its index in the
        # injected subset: a caller that injected hits 1 and 3 did not put the third
        # hit in second position.
        positions={hit.experience_id: ranked[hit.experience_id] for hit in selected},
        char_counts={hit.experience_id: len(renderer.format_hit(hit)) for hit in selected},
    )


def signal(
    runtime: AER,
    tracked: TrackedRetrievalResult,
    *,
    experience_id: str = "exp-1",
    value: UsageSignal = UsageSignal.ADOPTED,
    source: UsageSignalSource | None = UsageSignalSource.AGENT,
    override: bool = False,
) -> ExperienceUsage:
    """Record a usage signal for one experience of a tracked result."""
    return runtime.record_usage_signal(
        session_id=tracked.session_id,
        experience_id=experience_id,
        signal=value,
        source=source,
        override=override,
    )


def utility(
    runtime: AER,
    tracked: TrackedRetrievalResult,
    *,
    experience_id: str = "exp-1",
    value: UtilityLabel = UtilityLabel.HELPFUL,
    source: UtilitySource | None = UtilitySource.HUMAN,
    override: bool = False,
) -> ExperienceUsage:
    """Record a utility label for one experience of a tracked result."""
    return runtime.record_utility(
        session_id=tracked.session_id,
        experience_id=experience_id,
        label=value,
        source=source,
        override=override,
    )


def use_experience(
    runtime: AER,
    index: RecordingIndex,
    hit: object,
    *,
    run_id: str | None = None,
    value: UsageSignal | None = UsageSignal.ADOPTED,
    source: UsageSignalSource | None = UsageSignalSource.AGENT,
    utility_value: UtilityLabel | None = None,
    utility_source: UtilitySource | None = UtilitySource.HUMAN,
    injected: bool = True,
    query: str = DEFAULT_QUERY,
    domain: str | None = DEFAULT_DOMAIN,
) -> TrackedRetrievalResult:
    """One complete exposure: retrieve, inject, then say what happened.

    Takes the index hit rather than an experience id, so the experience is created
    once by the caller (``indexed_for``) and can be reused across the many exposures a
    promotion scenario needs. The shorthand for the tests that need "this experience
    was used in this run" without caring about the four API calls that express it.
    """
    experience_id: str = hit.experience_id
    tracked = retrieve(
        runtime,
        index,
        guidance=[hit],
        run_id=run_id,
        query=query,
        domain=domain,
    )
    if injected:
        inject(runtime, tracked, experience_ids=[experience_id])
    if value is not None:
        signal(runtime, tracked, experience_id=experience_id, value=value, source=source)
    if utility_value is not None:
        utility(
            runtime,
            tracked,
            experience_id=experience_id,
            value=utility_value,
            source=utility_source,
        )
    return tracked
