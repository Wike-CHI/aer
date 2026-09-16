"""Provenance, idempotency and duplicate merging (Milestone 5, sections 12-14, 41-42, 51-52).

What this file pins down is the promise that the store gets *better* with repetition
rather than larger: the same run distils once, and a recurring problem adds a source
to the knowledge that already exists instead of producing a near-identical twin.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import delete

from aer import (
    AER,
    Experience,
    ExperienceKind,
    ExperienceSource,
    ExperienceStatus,
    StorageError,
)
from aer.storage.models import ExperienceRow, RunRow
from tests.experience.support import (
    StaticProvider,
    build_false_success_run,
    build_recovery_run,
)


class TestOneThreeTwoRunsOneExperience:
    def test_a_run_produces_exactly_one_experience(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)

        assert experience is not None
        assert aer.experiences.count() == 1
        assert aer.get_experience_sources(experience.id) != []
        assert [source.run_id for source in aer.get_experience_sources(experience.id)] == [run_id]

    def test_the_experience_knows_which_run_produced_it(self, open_aer: Callable[..., AER]) -> None:
        """The source link is written in the same transaction as the experience, so
        knowledge can never exist without a run that justifies it."""
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)

        experience = aer.distill_run(run_id)
        assert experience is not None

        sources = aer.experience_sources.list_for_experience(experience.id)
        assert len(sources) == 1
        assert sources[0].run_id == run_id
        assert sources[0].created_at.tzinfo is not None


class TestIdempotency:
    def test_distilling_twice_returns_the_same_knowledge(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Section 42. Repeated calls must not inflate the store."""
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)

        first = aer.distill_run(run_id)
        second = aer.distill_run(run_id)

        assert first is not None
        assert second is not None
        assert first.id == second.id
        assert aer.experiences.count() == 1
        assert aer.experience_sources.count_for_experience(first.id) == 1

    def test_the_second_call_does_not_even_ask_the_provider(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Idempotency is checked before anything expensive happens."""
        provider = StaticProvider()
        aer = open_aer(provider)
        run_id = build_recovery_run(aer)

        aer.distill_run(run_id)
        calls = len(provider.calls)
        aer.distill_run(run_id)

        assert len(provider.calls) == calls

    def test_a_run_can_only_ever_be_linked_to_one_experience(
        self, open_aer: Callable[..., AER]
    ) -> None:
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)
        first = aer.distill_run(run_id)

        for _ in range(3):
            aer.distill_run(run_id)

        assert first is not None
        assert aer.get_experiences_for_run(run_id) == [first]

    def test_storing_the_same_link_twice_is_refused(self, open_aer: Callable[..., AER]) -> None:
        """The composite key is the last line of defence behind the service check."""
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)
        experience = aer.distill_run(run_id)
        assert experience is not None

        with pytest.raises(StorageError):
            aer.experience_sources.add(ExperienceSource(experience_id=experience.id, run_id=run_id))


class TestDuplicateMerging:
    def test_a_recurring_problem_joins_the_existing_experience(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Section 41: the same claim from a second run adds a source, not a twin."""
        aer = open_aer(StaticProvider())
        first_run = build_recovery_run(aer, task="the same task")
        second_run = build_recovery_run(aer, task="the same task")

        first = aer.distill_run(first_run)
        second = aer.distill_run(second_run)

        assert first is not None and second is not None
        assert first.id == second.id
        assert aer.experiences.count() == 1
        assert sorted(aer.experience_sources.get_runs(first.id)) == sorted([first_run, second_run])

    def test_a_genuinely_different_claim_gets_its_own_experience(
        self, open_aer: Callable[..., AER]
    ) -> None:
        aer = open_aer(StaticProvider())
        first = aer.distill_run(build_recovery_run(aer, task="task one"))
        second = aer.distill_run(build_false_success_run(aer, task="task two"))

        assert first is not None and second is not None
        assert first.id != second.id
        assert aer.experiences.count() == 2
        assert {first.kind, second.kind} == {ExperienceKind.RECOVERY, ExperienceKind.FAILURE}

    def test_a_deprecated_experience_is_not_revived_by_new_evidence(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """Withdrawal has to mean something: a withdrawn claim does not silently
        absorb new runs, it is left alone while a fresh record is created (D-034)."""
        aer = open_aer(StaticProvider())
        first_run = build_recovery_run(aer, task="the same task")
        old = aer.distill_run(first_run)
        assert old is not None

        aer.experiences.update(old.transition_to(ExperienceStatus.DEPRECATED, reason="wrong"))

        second_run = build_recovery_run(aer, task="the same task")
        fresh = aer.distill_run(second_run)

        assert fresh is not None
        assert fresh.id != old.id
        assert aer.experiences.count() == 1
        assert aer.experiences.count(include_deprecated=True) == 2
        assert aer.get_experience_sources(fresh.id)[0].run_id == second_run

    def test_a_reinforcing_run_can_promote_unverified_knowledge(
        self, open_aer: Callable[..., AER]
    ) -> None:
        """A claim first recorded from an unverified run is promoted once a run that
        a verifier confirmed supports the same claim. Without this the store would
        discard its own strongest evidence."""
        aer = open_aer(StaticProvider())
        unverified_run = build_recovery_run(aer, task="the same task", verify=False)
        verified_run = build_recovery_run(aer, task="the same task")

        first = aer.distill_run(unverified_run)
        assert first is not None
        assert first.status is ExperienceStatus.DISTILLED
        assert first.outcome_verified is False

        second = aer.distill_run(verified_run)

        assert second is not None
        assert second.id == first.id
        assert second.status is ExperienceStatus.VERIFIED
        assert second.outcome_verified is True
        assert aer.experience_sources.count_for_experience(first.id) == 2

    def test_merging_never_rewrites_the_claim_itself(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        first = aer.distill_run(build_recovery_run(aer, task="the same task"))
        assert first is not None

        merged = aer.distill_run(build_recovery_run(aer, task="the same task"))

        assert merged is not None
        assert merged.title == first.title
        assert merged.problem == first.problem
        assert merged.solution == first.solution
        assert merged.dedup_key == first.dedup_key
        assert merged.created_at == first.created_at
        # updated_at tracks changes to the knowledge; a new source is not one.
        assert merged.updated_at == first.updated_at


class TestSourceDeletion:
    def test_deleting_an_experience_cascades_to_its_sources(
        self, open_aer: Callable[..., AER]
    ) -> None:
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)
        experience = aer.distill_run(run_id)
        assert experience is not None

        with aer.database.session("delete experience") as session:
            session.execute(delete(ExperienceRow).where(ExperienceRow.id == experience.id))

        assert aer.experience_sources.get_runs(experience.id) == []
        assert aer.get_experiences_for_run(run_id) == []

    def test_deleting_a_run_cascades_to_its_sources(self, open_aer: Callable[..., AER]) -> None:
        """The run is gone, so the evidence behind the experience is gone: the link
        must not outlive it."""
        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)
        experience = aer.distill_run(run_id)
        assert experience is not None

        with aer.database.session("delete run") as session:
            session.execute(delete(RunRow).where(RunRow.id == run_id))

        assert aer.experience_sources.get_runs(experience.id) == []
        # The experience survives: its claim may still hold, it just lost a support.
        assert aer.get_experience(experience.id) is not None

    def test_a_link_can_be_removed_explicitly(self, open_aer: Callable[..., AER]) -> None:
        from aer import RecordNotFoundError

        aer = open_aer(StaticProvider())
        run_id = build_recovery_run(aer)
        experience = aer.distill_run(run_id)
        assert experience is not None

        assert aer.experience_sources.exists(experience.id, run_id) is True
        aer.experience_sources.remove(experience.id, run_id)
        assert aer.experience_sources.exists(experience.id, run_id) is False

        with pytest.raises(RecordNotFoundError, match="No source link"):
            aer.experience_sources.remove(experience.id, run_id)


class TestSourcesSurviveRestart:
    def test_experience_and_provenance_are_durable(self, open_aer: Callable[..., AER]) -> None:
        first = open_aer(StaticProvider())
        run_id = build_recovery_run(first)
        experience = first.distill_run(run_id)
        assert experience is not None

        second = open_aer(StaticProvider())
        reloaded = second.get_experience(experience.id)

        assert reloaded is not None
        assert reloaded == experience
        assert second.get_experience_sources(experience.id)[0].run_id == run_id
        assert second.get_experiences_for_run(run_id) == [reloaded]
        assert second.distill_run(run_id).id == experience.id  # type: ignore[union-attr]

    def test_listing_filters_by_kind_and_domain(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        aer.distill_run(build_recovery_run(aer, task="one"))
        aer.distill_run(build_false_success_run(aer, task="two"))

        recovery = aer.list_experiences(kind=ExperienceKind.RECOVERY)
        failures = aer.list_experiences(kind=ExperienceKind.FAILURE)

        assert len(recovery) == 1
        assert len(failures) == 1
        assert all(item.domain == "wordpress" for item in aer.list_experiences(domain="wordpress"))
        assert aer.list_experiences(domain="shopify") == []


class TestLookupByClaim:
    def test_finding_an_experience_by_its_claim(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        experience = aer.distill_run(build_recovery_run(aer))
        assert experience is not None

        found = aer.find_experiences(
            kind=experience.kind,
            domain=experience.domain,
            title=experience.title.upper(),
            problem=f"  {experience.problem}  ",
        )

        assert [item.id for item in found] == [experience.id]

    def test_a_different_claim_finds_nothing(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        aer.distill_run(build_recovery_run(aer))

        assert (
            aer.find_experiences(
                kind=ExperienceKind.SUCCESS,
                domain="wordpress",
                title="REST API 403 on page update",
                problem="Updating a page returns 403 and the H1 never changes: something else",
            )
            == []
        )

    def test_the_dedup_key_is_persisted(self, open_aer: Callable[..., AER]) -> None:
        aer = open_aer(StaticProvider())
        experience = aer.distill_run(build_recovery_run(aer))
        assert isinstance(experience, Experience)
        assert len(experience.dedup_key) == 64
