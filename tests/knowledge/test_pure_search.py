"""Read-only contract, volume limits and source preservation."""

import json
from types import SimpleNamespace

import pytest

from aer.exceptions import KnowledgeIndexUnavailable
from aer.knowledge.models import RetrievalHit, RetrievalResult
from aer.knowledge.search import search_experiences
from aer.runtime.enums import ExperienceKind, ExperienceStatus, RetrievalMode
from aer.runtime.serialization import utc_now


def hit(index=1):
    return RetrievalHit(
        experience_id=f"experience-{index}",
        kind=ExperienceKind.RECOVERY,
        status=ExperienceStatus.VERIFIED,
        domain="wordpress-operations",
        title="Field format",
        problem="Wrong field format",
        root_cause=None,
        solution="Check field return shape",
        failed_attempts=("Old mapping failed",),
        avoid=("Do not assume array",),
        outcome_verified=True,
        generalizable=True,
        source_count=1,
        created_at=utc_now(),
        updated_at=utc_now(),
        bm25_score=-1,
        text_score=1,
        trust_score=1,
        domain_score=1,
        freshness_score=1,
        retrieval_score=1,
    )


class ReadOnlyRuntime:
    """No mutation methods: accidental run/injection/index writes fail immediately."""

    def __init__(self, hits=()):
        self.hits = hits

    def retrieve(self, query, **kwargs):
        assert kwargs["mode"] == RetrievalMode.GUIDANCE
        return RetrievalResult(
            query=query, domain=kwargs["domain"], mode=kwargs["mode"], guidance=self.hits
        )

    def get_experience_sources(self, experience_id):
        return [SimpleNamespace(run_id="source-run")]


def test_contract_is_bounded_sourced_and_has_unknown_applicability():
    result = search_experiences(
        ReadOnlyRuntime(tuple(hit(i) for i in range(5))), "field", limit=5, max_chars=3000
    )
    assert result["schema_version"] == "aer.search.v1"
    assert result["status"] == "ready"
    assert len(json.dumps(result, ensure_ascii=False)) <= 3000
    assert result["injection_recorded"] is False
    for item in result["guidance"]:
        assert item["source_run_ids"] == ["source-run"]
        assert item["applicability"] == {"status": "unknown", "conditions": []}


def test_empty_is_distinct_from_index_error():
    assert search_experiences(ReadOnlyRuntime(), "field")["status"] == "no_match"

    class Broken(ReadOnlyRuntime):
        def retrieve(self, *args, **kwargs):
            raise KnowledgeIndexUnavailable("index offline")

    with pytest.raises(KnowledgeIndexUnavailable):
        search_experiences(Broken(), "field")


def test_long_summary_marked_and_untraceable_result_refused():
    runtime = ReadOnlyRuntime((hit().model_copy(update={"problem": "x" * 5000}),))
    result = search_experiences(runtime, "field")
    assert "[truncated]" in result["guidance"][0]["summary"]
    runtime.get_experience_sources = lambda _: []
    with pytest.raises(ValueError, match="source"):
        search_experiences(runtime, "field")


@pytest.mark.parametrize("kwargs", [{"limit": 6}, {"max_chars": 999}, {"max_chars": 8001}])
def test_invalid_bounds_rejected(kwargs):
    with pytest.raises(ValueError):
        search_experiences(ReadOnlyRuntime(), "field", **kwargs)


def test_real_index_from_captured_checked_run(tmp_path):
    pytest.importorskip("neug")
    from aer import AER
    from aer.projects.capture import capture
    from aer.projects.closeout import closeout
    from aer.projects.config import code_version
    from tests.projects.test_workflow import candidate, payload, project_at

    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    capture(
        project,
        payload(
            project,
            "PostToolUseFailure",
            tool_use_id="failed",
            tool_name="Bash",
            error="field mapping failed",
        ),
    )
    result = closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())[0]
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        aer.project_experiences()
        before_runs = [r.id for r in aer.list_runs()]
        before_usage = aer.experience_usage.count()
        response = search_experiences(aer, "field output", domain="wordpress")
        assert response["status"] == "ready"
        assert result["experience"]["id"] in [h["experience_id"] for h in response["guidance"]]
        assert response["guidance"][0]["source_run_ids"] == [run_id]
        assert [r.id for r in aer.list_runs()] == before_runs
        assert aer.experience_usage.count() == before_usage
