"""The usage repositories' read path, and what a hand-edited row does.

Every write goes through the domain model, so the invariants it enforces cannot be
violated by AER itself. A database that has been edited by hand, or restored from a
build that did not have those invariants, is a different matter -- and an audit store
has to report that as a storage problem rather than letting a validation error from the
model layer escape through a repository read.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aer import (
    AER,
    ExperienceUsage,
    RetrievalMode,
    RetrievalSession,
    StorageError,
    UsageRole,
    UsageSignal,
    UsageSignalSource,
    UtilityLabel,
    UtilitySource,
)


class TestUsageRowsRoundTrip:
    def test_every_field_survives_a_write_and_a_read(self, aer: AER) -> None:
        run = aer.start_run(task="a task")
        session = aer.retrieval_sessions.create(
            RetrievalSession(
                run_id=run.run_id,
                query_text="rest api 403",
                query_fingerprint="fp",
                domain="wordpress",
                mode=RetrievalMode.GUIDANCE,
                requested_limit=3,
                result_count=1,
                knowledge_projection_version=1,
                retrieval_policy_version="1",
                retrieval_duration_ms=7,
            )
        )
        experience = _store_experience(aer)
        stamp = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
        usage = aer.experience_usage.create(
            ExperienceUsage(
                retrieval_session_id=session.id,
                experience_id=experience.id,
                rank=1,
                role=UsageRole.WARNING,
                retrieval_score=0.75,
                retrieved_at=stamp,
                injected_at=stamp,
                injection_position=2,
                injection_chars=120,
                context_fingerprint="digest",
                formatter_version="1",
                usage_signal=UsageSignal.REJECTED,
                usage_signal_source=UsageSignalSource.HUMAN,
                usage_signal_at=stamp,
                utility_label=UtilityLabel.HARMFUL,
                utility_label_source=UtilitySource.EVALUATOR,
                utility_label_at=stamp,
            )
        )

        loaded = aer.experience_usage.get(usage.id)

        assert loaded == usage
        assert loaded is not None
        assert loaded.role is UsageRole.WARNING
        assert loaded.usage_signal_source is UsageSignalSource.HUMAN
        assert loaded.utility_label_source is UtilitySource.EVALUATOR


class TestAHandEditedRow:
    def test_it_is_reported_as_a_storage_problem_not_a_model_error(self, data_dir: Path) -> None:
        """A signal without its source cannot be written by AER -- only by hand.

        Reading it must fail the way every other unreadable row fails (``decode_enum``
        and the JSON loaders already raise :class:`StorageError`), because a caller
        catching storage problems should not have to also catch pydantic's error.
        """
        with AER(data_dir) as runtime:
            run = runtime.start_run(task="a task")
            session = runtime.retrieval_sessions.create(
                RetrievalSession(
                    run_id=run.run_id,
                    query_text="rest api 403",
                    query_fingerprint="fp",
                    mode=RetrievalMode.GUIDANCE,
                    requested_limit=3,
                    result_count=1,
                    retrieval_policy_version="1",
                )
            )
            experience = _store_experience(runtime)
            path = runtime.database.path

        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO experience_usage "
                "(id, retrieval_session_id, experience_id, rank, role, retrieval_score, "
                " retrieved_at, usage_signal, utility_label, created_at, updated_at) "
                "VALUES ('hand-edited', ?, ?, 1, 'GUIDANCE', 0.5, "
                "'2026-09-20 09:00:00.000000', 'ADOPTED', 'UNKNOWN', "
                "'2026-09-20 09:00:00.000000', '2026-09-20 09:00:00.000000')",
                (session.id, experience.id),
            )
            connection.commit()

        with AER(data_dir) as runtime, pytest.raises(StorageError, match="invariants"):
            runtime.experience_usage.get_by_session_and_experience(session.id, experience.id)


def _store_experience(runtime: AER):
    from aer import Experience, ExperienceKind, ExperienceStatus

    return runtime.experiences.create(
        Experience(
            id="exp-1",
            kind=ExperienceKind.RECOVERY,
            domain="wordpress",
            title="403",
            problem="403 on update",
            dedup_key="RECOVERY|wordpress|403|403 on update",
            status=ExperienceStatus.VERIFIED,
            outcome_verified=True,
        )
    )
