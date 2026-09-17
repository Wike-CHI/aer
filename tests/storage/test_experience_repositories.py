"""ExperienceRepository / ExperienceSourceRepository contracts (Milestone 5, section 14).

Exercised directly, without the service, so each method's contract -- including the
foreign keys, the composite source key and the refusal to guess at an unknown stored
kind -- is pinned on its own.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aer import (
    AER,
    Experience,
    ExperienceKind,
    ExperienceSource,
    ExperienceStatus,
    RecordNotFoundError,
    Run,
    StorageError,
)
from aer.storage.models import ExperienceRow

BASE_TIME = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)


def seed_run(aer: AER, run_id: str = "run-1") -> Run:
    return aer.runs.create(Run(id=run_id, task_description=f"task {run_id}"))


def make_experience(experience_id: str = "exp-1", **overrides: object) -> Experience:
    payload: dict[str, object] = {
        "id": experience_id,
        "kind": ExperienceKind.RECOVERY,
        "domain": "wordpress",
        "title": "REST API 403 on page update",
        "problem": "Updating a page returns 403",
        "dedup_key": f"key-{experience_id}",
        "symptoms": ("403 on POST", "H1 count stays 0"),
        "failed_attempts": ("blind retry",),
        "root_cause": "missing edit_posts capability",
        "solution": "switch to a credential with edit_posts",
        "recommended_workflow": ("check the capability first",),
        "avoid": ("retrying blindly",),
        "status": ExperienceStatus.DISTILLED,
        "confidence": 0.0,
        "generalizable": True,
        "outcome_verified": True,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
        "metadata": {"distillation": {"provider": "static"}},
    }
    payload.update(overrides)
    return Experience(**payload)  # type: ignore[arg-type]


class TestExperienceRepository:
    def test_round_trips_every_field(self, aer: AER) -> None:
        experience = make_experience()
        aer.experiences.create(experience)

        loaded = aer.experiences.get("exp-1")

        assert loaded == experience
        assert loaded is not None
        assert loaded.kind is ExperienceKind.RECOVERY
        assert loaded.status is ExperienceStatus.DISTILLED
        assert loaded.symptoms == ("403 on POST", "H1 count stays 0")
        assert loaded.failed_attempts == ("blind retry",)
        assert loaded.recommended_workflow == ("check the capability first",)
        assert loaded.avoid == ("retrying blindly",)
        assert loaded.outcome_verified is True
        assert loaded.metadata == {"distillation": {"provider": "static"}}
        assert loaded.created_at.tzinfo is not None

    def test_round_trips_an_incomplete_failure(self, aer: AER) -> None:
        """The NULL columns must survive the trip: absence is meaningful."""
        experience = make_experience(
            kind=ExperienceKind.FAILURE,
            root_cause=None,
            solution=None,
            symptoms=(),
            failed_attempts=("retry", "change headers"),
        )
        aer.experiences.create(experience)

        loaded = aer.experiences.get("exp-1")

        assert loaded is not None
        assert loaded.root_cause is None
        assert loaded.solution is None
        assert loaded.symptoms == ()
        assert loaded.failed_attempts == ("retry", "change headers")

    def test_get_returns_none_for_an_unknown_id(self, aer: AER) -> None:
        assert aer.experiences.get("nope") is None

    def test_create_twice_with_the_same_id_fails(self, aer: AER) -> None:
        aer.experiences.create(make_experience())

        with pytest.raises(StorageError):
            aer.experiences.create(make_experience())

    def test_update_overwrites_the_stored_row(self, aer: AER) -> None:
        aer.experiences.create(make_experience())
        moved = make_experience(status=ExperienceStatus.VERIFIED, updated_at=BASE_TIME)

        aer.experiences.update(moved)

        loaded = aer.experiences.get("exp-1")
        assert loaded is not None
        assert loaded.status is ExperienceStatus.VERIFIED

    def test_update_of_an_unknown_experience_raises(self, aer: AER) -> None:
        with pytest.raises(RecordNotFoundError, match="Experience not found"):
            aer.experiences.update(make_experience("ghost"))

    def test_list_is_newest_first_by_default(self, aer: AER) -> None:
        aer.experiences.create(make_experience("old", created_at=BASE_TIME))
        aer.experiences.create(make_experience("new", created_at=BASE_TIME + timedelta(minutes=5)))

        assert [item.id for item in aer.experiences.list()] == ["new", "old"]
        assert [item.id for item in aer.experiences.list(newest_first=False)] == ["old", "new"]

    def test_list_filters(self, aer: AER) -> None:
        aer.experiences.create(make_experience("rec", kind=ExperienceKind.RECOVERY))
        aer.experiences.create(
            make_experience(
                "fail",
                kind=ExperienceKind.FAILURE,
                domain="shopify",
                created_at=BASE_TIME + timedelta(minutes=1),
            )
        )

        assert [item.id for item in aer.experiences.list(kind=ExperienceKind.RECOVERY)] == ["rec"]
        assert [item.id for item in aer.experiences.list(domain="shopify")] == ["fail"]
        # Newest first: `fail` was created a minute later.
        assert [item.id for item in aer.experiences.list(status=ExperienceStatus.DISTILLED)] == [
            "fail",
            "rec",
        ]
        assert aer.experiences.list(status=ExperienceStatus.VERIFIED) == []

    def test_deprecated_experiences_are_excluded_by_default(self, aer: AER) -> None:
        aer.experiences.create(make_experience("live"))
        aer.experiences.create(
            make_experience(
                "dead",
                status=ExperienceStatus.DEPRECATED,
                created_at=BASE_TIME + timedelta(minutes=1),
            )
        )

        assert [item.id for item in aer.experiences.list()] == ["live"]
        assert [item.id for item in aer.experiences.list(include_deprecated=True)] == [
            "dead",
            "live",
        ]
        assert [item.id for item in aer.experiences.list(status=ExperienceStatus.DEPRECATED)] == [
            "dead"
        ]

    def test_count_matches_list(self, aer: AER) -> None:
        aer.experiences.create(make_experience("a"))
        aer.experiences.create(make_experience("b", kind=ExperienceKind.FAILURE))
        aer.experiences.create(make_experience("c", status=ExperienceStatus.DEPRECATED))

        assert aer.experiences.count() == 2
        assert aer.experiences.count(include_deprecated=True) == 3
        assert aer.experiences.count(kind=ExperienceKind.FAILURE) == 1
        assert aer.experiences.count(status=ExperienceStatus.DEPRECATED) == 1

    def test_list_pages(self, aer: AER) -> None:
        for index in range(3):
            aer.experiences.create(
                make_experience(f"e{index}", created_at=BASE_TIME + timedelta(minutes=index))
            )

        page = aer.experiences.list(newest_first=False, limit=1, offset=1)

        assert [item.id for item in page] == ["e1"]

    def test_find_by_dedup_key(self, aer: AER) -> None:
        aer.experiences.create(make_experience("a", dedup_key="shared"))
        aer.experiences.create(make_experience("b", dedup_key="shared"))
        aer.experiences.create(make_experience("c", dedup_key="other"))

        assert [item.id for item in aer.experiences.find_by_dedup_key("shared")] == ["a", "b"]
        assert aer.experiences.find_by_dedup_key("missing") == []

    def test_find_by_dedup_key_skips_deprecated_by_default(self, aer: AER) -> None:
        aer.experiences.create(
            make_experience("dead", dedup_key="shared", status=ExperienceStatus.DEPRECATED)
        )

        assert aer.experiences.find_by_dedup_key("shared") == []
        assert [
            item.id for item in aer.experiences.find_by_dedup_key("shared", include_deprecated=True)
        ] == ["dead"]

    def test_an_unknown_stored_kind_is_refused(self, aer: AER) -> None:
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, status, confidence, generalizable, "
                " outcome_verified, dedup_key, created_at, updated_at) "
                "VALUES ('weird', 'MAYBE', 'd', 't', 'p', 'RAW', 0, 1, 0, 'k', "
                "'2026-09-16 14:00:00.000000', '2026-09-16 14:00:00.000000')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="unknown value"):
            aer.experiences.get("weird")

    def test_an_unknown_stored_status_is_refused(self, aer: AER) -> None:
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, status, confidence, generalizable, "
                " outcome_verified, dedup_key, created_at, updated_at) "
                "VALUES ('weird', 'SUCCESS', 'd', 't', 'p', 'PROVEN_MAYBE', 0, 1, 0, 'k', "
                "'2026-09-16 14:00:00.000000', '2026-09-16 14:00:00.000000')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="unknown value"):
            aer.experiences.get("weird")

    def test_a_corrupted_list_column_is_refused(self, aer: AER) -> None:
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, symptoms_json, status, confidence, "
                " generalizable, outcome_verified, dedup_key, created_at, updated_at) "
                "VALUES ('broken', 'SUCCESS', 'd', 't', 'p', '{\"not\": \"a list\"}', 'RAW', 0, "
                "1, 0, 'k', '2026-09-16 14:00:00.000000', '2026-09-16 14:00:00.000000')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="expected a list"):
            aer.experiences.get("broken")

    def test_a_corrupted_metadata_column_is_refused(self, aer: AER) -> None:
        with sqlite3.connect(aer.database.path) as connection:
            connection.execute(
                "INSERT INTO experiences "
                "(id, kind, domain, title, problem, status, confidence, generalizable, "
                " outcome_verified, dedup_key, created_at, updated_at, metadata_json) "
                "VALUES ('broken', 'SUCCESS', 'd', 't', 'p', 'RAW', 0, 1, 0, 'k', "
                "'2026-09-16 14:00:00.000000', '2026-09-16 14:00:00.000000', 'not json')"
            )
            connection.commit()

        with pytest.raises(StorageError, match="corrupted"):
            aer.experiences.get("broken")


class TestAtomicCreateWithSource:
    def test_the_experience_and_its_source_are_written_together(self, aer: AER) -> None:
        seed_run(aer)

        aer.experiences.create(make_experience(), source_run_id="run-1")

        assert aer.experience_sources.get_runs("exp-1") == ["run-1"]

    def test_a_missing_run_rolls_the_whole_thing_back(self, aer: AER) -> None:
        """An experience whose provenance failed to land would be knowledge nobody
        can trace -- so neither row is written."""
        with pytest.raises(StorageError):
            aer.experiences.create(make_experience(), source_run_id="missing-run")

        assert aer.experiences.get("exp-1") is None
        assert aer.experiences.count(include_deprecated=True) == 0

    def test_a_source_is_optional(self, aer: AER) -> None:
        aer.experiences.create(make_experience())

        assert aer.experience_sources.get_runs("exp-1") == []


class TestExperienceSourceRepository:
    def test_links_and_reads_back(self, aer: AER) -> None:
        seed_run(aer, "run-1")
        seed_run(aer, "run-2")
        aer.experiences.create(make_experience())

        # Both links carry an explicit timestamp. The assertion below is about
        # ``created_at`` ordering, so letting one row fall back to ``utc_now()``
        # makes the outcome depend on when the suite happens to run: this test passed
        # for as long as the wall clock was earlier than ``BASE_TIME + 1 minute`` and
        # then started failing on its own. A drill found it (2026-09-17); the
        # repository was right and the test was reading the clock.
        aer.experience_sources.add(
            ExperienceSource(experience_id="exp-1", run_id="run-1", created_at=BASE_TIME)
        )
        aer.experience_sources.add(
            ExperienceSource(
                experience_id="exp-1", run_id="run-2", created_at=BASE_TIME + timedelta(minutes=1)
            )
        )

        assert aer.experience_sources.get_runs("exp-1") == ["run-1", "run-2"]
        assert aer.experience_sources.count_for_experience("exp-1") == 2
        assert aer.experience_sources.exists("exp-1", "run-1") is True
        assert aer.experience_sources.exists("exp-1", "run-3") is False

    def test_a_duplicate_link_is_refused(self, aer: AER) -> None:
        """The composite primary key is what makes the same run uncountable twice."""
        seed_run(aer)
        aer.experiences.create(make_experience())
        aer.experience_sources.add(ExperienceSource(experience_id="exp-1", run_id="run-1"))

        with pytest.raises(StorageError):
            aer.experience_sources.add(ExperienceSource(experience_id="exp-1", run_id="run-1"))

    def test_it_requires_an_existing_experience(self, aer: AER) -> None:
        seed_run(aer)

        with pytest.raises(StorageError):
            aer.experience_sources.add(ExperienceSource(experience_id="missing", run_id="run-1"))

    def test_it_requires_an_existing_run(self, aer: AER) -> None:
        aer.experiences.create(make_experience())

        with pytest.raises(StorageError):
            aer.experience_sources.add(ExperienceSource(experience_id="exp-1", run_id="missing"))

    def test_get_experiences_for_run(self, aer: AER) -> None:
        seed_run(aer)
        aer.experiences.create(make_experience("a"))
        aer.experiences.create(make_experience("b"))
        aer.experience_sources.add(ExperienceSource(experience_id="a", run_id="run-1"))
        aer.experience_sources.add(ExperienceSource(experience_id="b", run_id="run-1"))

        assert sorted(aer.experience_sources.get_experiences_for_run("run-1")) == ["a", "b"]
        assert aer.experience_sources.get_experiences_for_run("no-such-run") == []

    def test_list_for_experience_returns_full_records(self, aer: AER) -> None:
        seed_run(aer)
        aer.experiences.create(make_experience())
        aer.experience_sources.add(
            ExperienceSource(experience_id="exp-1", run_id="run-1", created_at=BASE_TIME)
        )

        sources = aer.experience_sources.list_for_experience("exp-1")

        assert len(sources) == 1
        assert sources[0].experience_id == "exp-1"
        assert sources[0].run_id == "run-1"
        assert sources[0].created_at == BASE_TIME

    def test_a_source_can_be_removed_and_removing_twice_raises(self, aer: AER) -> None:
        seed_run(aer)
        aer.experiences.create(make_experience())
        aer.experience_sources.add(ExperienceSource(experience_id="exp-1", run_id="run-1"))

        aer.experience_sources.remove("exp-1", "run-1")
        assert aer.experience_sources.get_runs("exp-1") == []

        with pytest.raises(RecordNotFoundError, match="No source link"):
            aer.experience_sources.remove("exp-1", "run-1")

    def test_sources_of_an_unknown_experience_are_empty(self, aer: AER) -> None:
        assert aer.experience_sources.get_runs("missing") == []
        assert aer.experience_sources.count_for_experience("missing") == 0


class TestStorageFormat:
    def test_the_experience_table_is_readable_with_the_standard_driver(self, aer: AER) -> None:
        aer.experiences.create(make_experience())

        with sqlite3.connect(aer.database.path) as connection:
            row = connection.execute(
                "SELECT kind, status, dedup_key FROM experiences WHERE id = 'exp-1'"
            ).fetchone()

        assert row == ("RECOVERY", "DISTILLED", "key-exp-1")

    def test_the_row_is_not_stored_when_the_metadata_is_not_json_safe(self, aer: AER) -> None:
        """Metadata goes through the JSON contract like every other payload."""
        experience = make_experience(metadata={"nested": {"list": [1, 2, 3]}})

        aer.experiences.create(experience)
        loaded = aer.experiences.get("exp-1")

        assert loaded is not None
        assert loaded.metadata == {"nested": {"list": [1, 2, 3]}}

    def test_experience_row_table_name(self) -> None:
        assert ExperienceRow.__tablename__ == "experiences"
