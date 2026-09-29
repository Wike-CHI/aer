"""Milestone 7 acceptance: the two scenarios the milestone is judged on, plus the
operational properties that decide whether the numbers can be trusted.

The two scenarios (round-7 brief, sections 78-79) are the same experience used twice:

* **positive** -- retrieved, injected, adopted, verified success. One retrieval, one
  injection, one adoption, one verified success;
* **negative** -- the same experience retrieved, injected and adopted again, into a run
  that then failed verification. Two retrievals, two injections, two adoptions, one
  verified success and one verified failure, and an *observed* adopted success rate of
  0.5 -- which is a description of two data points, not a claim that the experience
  works half the time.

Three further properties are checked here because they only show up in an assembled
system: the analytics survive a restart, they are independent of the knowledge index,
and they do not issue a query per experience.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event

from aer import (
    AER,
    KnowledgeIndexUnavailable,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.usage.fingerprints import context_fingerprint
from tests.knowledge.support import RecordingIndex
from tests.usage.support import (
    indexed_for,
    retrieve,
    use_experience,
    verified_failure_run,
    verified_success_run,
)


def scenario_runtime(data_dir: Path, index: RecordingIndex) -> AER:
    return AER(data_dir, knowledge_index=index)


@pytest.fixture
def index() -> RecordingIndex:
    """A recording, non-scoring stand-in for the graph index.

    Defined here rather than shared with ``tests/usage``: these tests assemble a whole
    runtime and the fixture is one line, while a cross-directory fixture would couple
    two suites' setup for no benefit.
    """
    return RecordingIndex()


class TestPositiveScenario:
    def test_a_reused_experience_reports_one_of_everything(
        self, data_dir: Path, index: RecordingIndex
    ) -> None:
        """Section 78: Experience A from Run A, used successfully in Run B."""
        with scenario_runtime(data_dir, index) as aer:
            source = verified_success_run(aer, run_id="run-a")
            hit = indexed_for(aer, source_run_id=source)

            target = verified_success_run(aer, run_id="run-b")
            tracked = retrieve(aer, index, guidance=[hit], run_id=target)
            hit_row = tracked.result.all_hits[0]
            aer.record_injection(
                session_id=tracked.session_id,
                experience_ids=[hit_row.experience_id],
                context_fingerprint=context_fingerprint(
                    ExperienceContextFormatter().format(tracked.result)
                ),
                formatter_version=FORMATTER_VERSION,
                positions={hit_row.experience_id: 1},
                char_counts={
                    hit_row.experience_id: len(ExperienceContextFormatter().format_hit(hit_row))
                },
            )
            aer.record_usage_signal(
                session_id=tracked.session_id,
                experience_id="exp-1",
                signal=UsageSignal.ADOPTED,
                source=UsageSignalSource.AGENT,
            )
            aer.record_utility(
                session_id=tracked.session_id,
                experience_id="exp-1",
                label=UtilityLabel.HELPFUL,
                source=UtilitySource.HUMAN,
            )

            report = aer.experience_effectiveness("exp-1")

            assert report.retrieval_count == 1
            assert report.injection_count == 1
            assert report.explicit_adoption_count == 1
            assert report.verified_success_runs == 1
            assert report.verified_failure_runs == 0
            assert report.observed_success_rate == 1.0
            assert report.adopted_verified_success_rate == 1.0
            assert report.distinct_target_runs == 1
            assert report.helpful_count == 1
            assert report.harmful_count == 0

            # And the lifecycle follows from the evidence, not from the success.
            assert aer.promote_experience("exp-1") is not None
            promoted = aer.get_experience("exp-1")
            assert promoted is not None
            assert promoted.status.value == "REUSED"


class TestNegativeScenario:
    def test_a_success_and_a_failure_report_half_and_say_so(
        self, data_dir: Path, index: RecordingIndex
    ) -> None:
        """Section 79: adoption plus failure is still a failure for that run."""
        with scenario_runtime(data_dir, index) as aer:
            source = verified_success_run(aer, run_id="run-a")
            hit = indexed_for(aer, source_run_id=source)

            use_experience(
                aer,
                index,
                hit,
                run_id=verified_success_run(aer, run_id="run-b"),
            )
            use_experience(
                aer,
                index,
                hit,
                run_id=verified_failure_run(aer, run_id="run-c"),
            )

            report = aer.experience_effectiveness("exp-1")

            assert report.retrieval_count == 2
            assert report.injection_count == 2
            assert report.explicit_adoption_count == 2
            assert report.distinct_target_runs == 2
            assert report.verified_success_runs == 1
            assert report.verified_failure_runs == 1
            assert report.observed_success_rate == 0.5
            assert report.adopted_verified_success_rate == 0.5

            # The report describes two observations and refuses to conclude from them:
            # one success and one failure is not enough for the promotion policy.
            assert aer.promote_experience("exp-1") is not None  # REUSED, and only that
            decision = aer.evaluate_promotion("exp-1")
            assert decision.eligible is False
            assert decision.suggested_status is None

    def test_the_report_names_the_rate_for_what_it_is(self, data_dir: Path) -> None:
        """The name is part of the contract (sections 28-30).

        An observation among runs that happened to have the experience present is
        ``observed_success_rate``. There is deliberately no field called ``effect``,
        because a field with that name on this table would be a causal claim this
        milestone cannot support.
        """
        with scenario_runtime(data_dir, RecordingIndex()) as aer:
            indexed_for(aer)

            report = aer.experience_effectiveness("exp-1")
            fields = set(type(report).__dataclass_fields__)

            assert "observed_success_rate" in fields
            assert "injected_verified_success_rate" in fields
            assert "adopted_verified_success_rate" in fields
            assert not {name for name in fields if "effect" in name or "causal" in name}


class TestRestartPersistence:
    def test_everything_survives_a_restart(self, data_dir: Path) -> None:
        """Section 75: sessions, rows, signals and feedback all come back."""
        index = RecordingIndex()
        with scenario_runtime(data_dir, index) as first:
            hit = indexed_for(first)
            tracked = use_experience(
                first,
                index,
                hit,
                run_id=verified_success_run(first),
                utility_value=UtilityLabel.HELPFUL,
            )
            session_id = tracked.session_id
            first_report = first.experience_effectiveness("exp-1")

        with scenario_runtime(data_dir, RecordingIndex()) as second:
            session = second.get_retrieval_session(session_id)
            assert session is not None
            assert session.query_text
            rows = second.get_session_usage(session_id)
            assert len(rows) == 1
            assert rows[0].is_injected is True
            assert rows[0].usage_signal is UsageSignal.ADOPTED
            assert rows[0].utility_label is UtilityLabel.HELPFUL
            assert rows[0].context_fingerprint is not None
            assert second.experience_effectiveness("exp-1") == first_report


class TestTheAnalyticsDoNotDependOnTheKnowledgeIndex:
    def test_existing_usage_is_readable_when_the_index_is_down(self, data_dir: Path) -> None:
        """Section 85: usage lives in SQLite, so a broken index cannot take it away."""
        index = RecordingIndex()
        with scenario_runtime(data_dir, index) as aer:
            hit = indexed_for(aer)
            use_experience(aer, index, hit, run_id=verified_success_run(aer))

        with scenario_runtime(data_dir, _UnavailableIndex()) as aer:
            report = aer.experience_effectiveness("exp-1")
            assert report.verified_success_runs == 1
            assert aer.experience_confidence("exp-1").confidence > 0.0
            assert aer.list_retrieval_sessions()

            with pytest.raises(KnowledgeIndexUnavailable):
                aer.retrieve_for_run("anything", domain="wordpress")

            with pytest.raises(KnowledgeIndexUnavailable):
                aer.retrieve("anything", domain="wordpress")


class TestTheReportIsNotAnNPlusOne:
    def test_the_number_of_queries_does_not_grow_with_the_data(
        self, data_dir: Path, index: RecordingIndex
    ) -> None:
        """Section 84: look for the N+1, without inventing an SLA.

        The assertion is "the query count is the same for one experience as for five",
        which is the property that matters. An absolute number would be a promise
        about a query planner rather than about this code.
        """
        with scenario_runtime(data_dir, index) as aer:
            statements: list[str] = []

            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            event.listen(aer.database.engine, "before_cursor_execute", record)

            hit = indexed_for(aer, experience_id="exp-1")
            use_experience(
                aer,
                index,
                hit,
                run_id=verified_success_run(aer, run_id="run-1"),
            )

            before = len(statements)
            reports = aer.experience_effectiveness_all()
            small = len(statements) - before
            assert [report.experience_id for report in reports] == ["exp-1"]

            for position in range(2, 7):
                more = indexed_for(aer, experience_id=f"exp-{position}")
                use_experience(
                    aer,
                    index,
                    more,
                    run_id=verified_success_run(aer, run_id=f"run-{position}"),
                )

            before = len(statements)
            reports = aer.experience_effectiveness_all()
            large = len(statements) - before

            assert len(reports) == 6
            assert large == small, (
                f"six experiences cost {large} queries against {small} for one, which "
                "is the shape of an N+1"
            )

    def test_a_batch_of_reports_costs_the_same_as_one(
        self, data_dir: Path, index: RecordingIndex
    ) -> None:
        with scenario_runtime(data_dir, index) as aer:
            statements: list[str] = []

            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            event.listen(aer.database.engine, "before_cursor_execute", record)

            for position in range(5):
                hit = indexed_for(aer, experience_id=f"exp-{position}")
                use_experience(
                    aer,
                    index,
                    hit,
                    run_id=verified_success_run(aer, run_id=f"run-{position}"),
                )

            before = len(statements)
            aer.effectiveness.reports([f"exp-{position}" for position in range(5)])
            batched = len(statements) - before

            before = len(statements)
            aer.effectiveness.reports(["exp-0"])
            single = len(statements) - before

            assert batched == single


class _UnavailableIndex:
    """An index that is up enough to report its version but cannot answer a query.

    Models the realistic failure: the file is there, the schema version is known, and
    ``LOAD``/``FTS`` fails at query time. Retrieval must surface that rather than
    return "nothing found" (round-6 brief, section 80).
    """

    def __init__(self) -> None:
        self.path = "<unavailable>"

    def ensure_schema(self) -> None:
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def projection_version(self) -> int | None:
        return None

    def upsert_experiences(self, experiences, run_refs) -> None:
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def delete_experiences(self, experience_ids) -> None:
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def search(self, query):
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def source_counts(self, experience_ids):
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def fingerprints(self):
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def count_experiences(self, *, include_deprecated: bool = True) -> int:
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def reset(self) -> None:
        raise KnowledgeIndexUnavailable("the knowledge engine is missing")

    def close(self) -> None:
        return None
