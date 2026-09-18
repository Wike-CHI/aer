"""The operator surface: what ``status`` reports and what the CLI returns.

These tests matter more than they look. ``status`` is the command someone runs when
retrieval is producing nothing, and the two failure modes it must never have are
crashing and answering "fine". Both are checked here.
"""

from __future__ import annotations

import pytest

from aer.exceptions import ConfigurationError, KnowledgeIndexUnavailable
from aer.knowledge import cli
from aer.knowledge.projector import DriftReport
from aer.knowledge.status import KnowledgeStatus, collect_status
from tests.knowledge.conftest import store_experience


class TestStatus:
    def test_a_synced_index_reports_no_drift(self, knowledge_runtime, knowledge_index) -> None:
        store_experience(knowledge_runtime)
        knowledge_runtime.knowledge_projector.project_all()

        status = knowledge_runtime.knowledge_status()

        assert status.in_sync is True
        assert status.reachable is True
        assert status.store_experiences == 1
        assert status.index_experiences == 1
        assert status.projection_schema_version == 1
        assert "drift              : none" in status.describe()

    def test_an_unprojected_store_reports_the_missing_records(self, knowledge_runtime) -> None:
        store_experience(knowledge_runtime, experience_id="exp-1")
        status = knowledge_runtime.knowledge_status()
        assert status.in_sync is False
        assert status.drift is not None
        assert status.drift.missing == ("exp-1",)
        assert "drift              : 1" in status.describe()
        assert "missing: exp-1" in status.describe()

    def test_an_unreachable_index_is_reported_rather_than_raised(
        self, knowledge_runtime, knowledge_index
    ) -> None:
        """The one place a broken index must not be an exception.

        A status command that dies from the failure it was invoked to describe has
        told the operator nothing at all.
        """
        knowledge_index._fail_on_ensure = KnowledgeIndexUnavailable("engine is missing")
        status = knowledge_runtime.knowledge_status()

        assert status.reachable is False
        assert status.in_sync is False
        assert "engine is missing" in status.detail
        assert "reachable          : NO" in status.describe()

    def test_drift_details_are_summarised_not_dumped(self) -> None:
        status = KnowledgeStatus(
            database_path="/knowledge/aer-knowledge",
            reachable=True,
            projection_schema_version=1,
            store_experiences=100,
            index_experiences=90,
            drift=DriftReport(
                store_count=100,
                index_count=90,
                missing=tuple(f"exp-{i}" for i in range(20)),
                stale=(),
                orphaned=(),
            ),
            detail="",
        )
        rendered = status.describe()
        assert "drift              : 20" in rendered
        assert "exp-0, exp-1" in rendered
        assert "(+15)" in rendered
        assert "exp-19" not in rendered

    def test_an_in_memory_index_reports_a_placeholder_path(
        self, projector, knowledge_index
    ) -> None:
        status = collect_status(knowledge_index, projector, store_experiences=0)
        assert status.database_path == "<in-memory>"

    def test_a_non_aer_error_is_still_reported(self, knowledge_runtime, knowledge_index) -> None:
        """A status command must survive an unexpected exception too."""
        knowledge_index._fail_on_ensure = ValueError("something odd")
        status = knowledge_runtime.knowledge_status()
        assert status.reachable is False
        assert "ValueError" in status.detail


class TestParser:
    def test_it_offers_exactly_the_three_commands(self) -> None:
        parser = cli.build_parser()
        actions = [
            action for action in parser._actions if hasattr(action, "choices") and action.choices
        ]
        assert set(actions[0].choices) == {"status", "rebuild", "project"}

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])


class TestMain:
    def test_status_runs_and_prints_a_report(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("AER_ENV", "development")
        monkeypatch.setenv("AER_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AER_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        monkeypatch.delenv("AER_DB_PATH", raising=False)

        code = cli.main(["status"])

        assert code == 0

    def test_a_configuration_error_becomes_exit_1(self, monkeypatch) -> None:
        def explode() -> None:
            raise ConfigurationError("AER_ENV=production requires absolute paths")

        monkeypatch.setattr(cli, "load_deployment_config", explode)
        assert cli.main(["status"]) == 1

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["frobnicate"])
        assert exit_info.value.code == 2


class TestReachableIndexPath:
    """The parts of ``status``/``rebuild`` that do not need a real engine."""

    def test_rebuild_reports_what_it_did(self, knowledge_runtime, knowledge_index) -> None:
        store_experience(knowledge_runtime, experience_id="exp-1")
        report = knowledge_runtime.rebuild_knowledge()
        assert report.projected == 1
        assert "1 experiences projected" in report.description

    def test_project_experiences_is_the_incremental_catch_up(
        self, knowledge_runtime, knowledge_index
    ) -> None:
        store_experience(knowledge_runtime, experience_id="exp-1")
        store_experience(knowledge_runtime, experience_id="exp-2", title="t2")
        assert knowledge_runtime.project_experiences() == 2
        assert knowledge_runtime.knowledge_status().in_sync is True

    def test_a_closed_runtime_refuses_knowledge_work(self, data_dir, knowledge_index) -> None:
        from aer import AER
        from aer.exceptions import AERError

        runtime = AER(data_dir, knowledge_index=knowledge_index)
        runtime.close()
        with pytest.raises(AERError, match="closed"):
            runtime.retrieve("anything")

    def test_an_injected_index_is_left_open_for_its_owner(self, data_dir, knowledge_index) -> None:
        """Closing a runtime must not close a resource it did not open.

        An injected index belongs to whoever injected it; a runtime that disposed of
        it would break the next test, or the caller's next use, with a confusing
        error.
        """
        from aer import AER

        runtime = AER(data_dir, knowledge_index=knowledge_index)
        _ = runtime.knowledge_index
        runtime.close()
        assert knowledge_index.closed == 0

    def test_an_index_the_runtime_built_is_closed(self, data_dir, knowledge_index, monkeypatch):
        """The other half of the ownership rule, on the path production takes."""
        from aer import AER
        from aer.knowledge import neug

        monkeypatch.setattr(neug, "NeuGKnowledgeIndex", lambda *a, **k: knowledge_index)

        runtime = AER(data_dir)
        _ = runtime.knowledge_index
        runtime.close()
        assert knowledge_index.closed == 1

    def test_a_runtime_that_never_retrieved_never_touches_the_engine(
        self, data_dir, monkeypatch
    ) -> None:
        """Section 79: an unavailable knowledge plane must not break startup."""
        from aer import AER
        from aer.knowledge import neug

        def explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("the engine must not be constructed")

        monkeypatch.setattr(neug, "NeuGKnowledgeIndex", explode)

        runtime = AER(data_dir)
        run = runtime.start_run(task="no knowledge needed")
        run.success()
        runtime.close()
