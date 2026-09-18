"""The parts of the NeuG adapter that do not need the engine.

``aer.knowledge.neug`` imports the engine lazily, on purpose, so everything except
``_connect`` can be exercised on a machine that has no NeuG wheel at all. That
includes the two functions that turned out to be wrong in the first version of this
milestone -- a row decoder whose off-by-one made *every* search raise, and a
statement builder whose output the engine would not accept. Both were invisible
locally because the only tests that touched them needed an engine to produce a row.

Anywhere a pure function can carry the risk instead of the engine, it should.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from aer.exceptions import KnowledgeIndexUnavailable, KnowledgeSchemaError, ProjectionError
from aer.knowledge.base import IndexFilters
from aer.knowledge.neug import (
    _MATCH_COLUMNS,
    NeuGKnowledgeIndex,
    _import_neug,
    _to_match,
    _where_clause,
)
from aer.knowledge.schema import (
    EXPERIENCE_INDEXED_PROPERTIES,
    FTS_INDEX_NAME,
    SCHEMA_STATEMENTS,
    fts_index_statement,
)

#: A row exactly as the search query returns it: the selected properties in the
#: order the schema declares, then the BM25 score.
ROW = (
    "exp-1",  # id
    "RECOVERY",  # kind
    "VERIFIED",  # status
    "wordpress",  # domain
    "WordPress REST API 403",  # title
    "应用密码无效",  # problem
    "权限不足",  # root_cause
    "改用有 edit_posts 权限的密码",  # solution
    "盲目重试",  # failed_attempts_text
    "不要盲目重试",  # avoid_text
    True,  # outcome_verified
    True,  # generalizable
    "2026-09-01T00:00:00+00:00",  # created_at
    "2026-09-02T00:00:00+00:00",  # updated_at
    -2.5,  # score
)


class TestRowDecoding:
    """The decoder that made every retrieval fail, kept honest by a literal row."""

    def test_it_reads_a_well_formed_row(self) -> None:
        match = _to_match(ROW)
        assert match.experience_id == "exp-1"
        assert match.kind == "RECOVERY"
        assert match.status == "VERIFIED"
        assert match.domain == "wordpress"
        assert match.title == "WordPress REST API 403"
        assert match.root_cause == "权限不足"
        assert match.solution == "改用有 edit_posts 权限的密码"
        assert match.failed_attempts_text == "盲目重试"
        assert match.avoid_text == "不要盲目重试"
        assert match.outcome_verified is True
        assert match.generalizable is True
        assert match.created_at == "2026-09-01T00:00:00+00:00"
        assert match.updated_at == "2026-09-02T00:00:00+00:00"

    def test_the_score_column_is_not_treated_as_a_property(self) -> None:
        """The bug: fourteen names paired against fifteen values.

        ``zip(strict=True)`` turned that into a ``ValueError`` on every search, and
        nothing local noticed because producing the row needs an engine.
        """
        assert _to_match(ROW).bm25_score == -2.5

    def test_the_score_keeps_its_sign(self) -> None:
        """BM25 is negative; the raw value is carried through untranslated."""
        assert _to_match((*ROW[:-1], -0.25)).bm25_score == -0.25

    def test_a_row_with_too_few_columns_is_refused(self) -> None:
        from aer.exceptions import ProjectionError

        with pytest.raises(ProjectionError, match="row shape changed"):
            _to_match(ROW[:-2])

    def test_a_row_with_too_many_columns_is_refused(self) -> None:
        from aer.exceptions import ProjectionError

        with pytest.raises(ProjectionError, match="row shape changed"):
            _to_match((*ROW, "extra"))

    def test_the_column_order_matches_the_schema(self) -> None:
        """The query and the decoder read the same list, so they cannot drift."""
        assert _MATCH_COLUMNS == (
            "id",
            "kind",
            "status",
            "domain",
            "title",
            "problem",
            "root_cause",
            "solution",
            "failed_attempts_text",
            "avoid_text",
            "outcome_verified",
            "generalizable",
            "created_at",
            "updated_at",
        )

    def test_every_indexed_property_is_selected(self) -> None:
        """A property the full-text index can search must also be readable.

        Otherwise BM25 could rank on a field the formatter never sees, and a hit
        would be returned with the evidence for its score missing.
        """
        for name in EXPERIENCE_INDEXED_PROPERTIES:
            assert name in _MATCH_COLUMNS


class TestWhereClause:
    """Policy filters become Cypher conditions plus parameters."""

    def test_no_filters_produce_no_conditions(self) -> None:
        conditions, parameters = _where_clause(IndexFilters())
        assert conditions == ""
        assert parameters == {}

    def test_kinds_and_statuses_become_in_clauses(self) -> None:
        conditions, parameters = _where_clause(
            IndexFilters(kinds=("SUCCESS", "RECOVERY"), statuses=("VERIFIED",))
        )
        assert conditions == "e.kind IN $kinds AND e.status IN $statuses"
        assert parameters == {"kinds": ["SUCCESS", "RECOVERY"], "statuses": ["VERIFIED"]}

    def test_the_boolean_filter_is_a_parameter(self) -> None:
        conditions, parameters = _where_clause(IndexFilters(outcome_verified=True))
        assert conditions == "e.outcome_verified = $outcome_verified"
        assert parameters == {"outcome_verified": True}

    def test_an_empty_tuple_is_expressed_rather_than_dropped(self) -> None:
        """`IN []` matches nothing, which is what the caller asked for.

        Dropping the condition would widen the query instead, which is the one
        direction a filter must never fail in.
        """
        conditions, parameters = _where_clause(IndexFilters(kinds=()))
        assert conditions == "e.kind IN $kinds"
        assert parameters == {"kinds": []}

    def test_deprecation_is_expressed_through_the_status_set(self) -> None:
        """No separate flag, so the status set is the only authority."""
        conditions, parameters = _where_clause(IndexFilters(statuses=("VERIFIED", "DEPRECATED")))
        assert "DEPRECATED" in parameters["statuses"]
        assert conditions == "e.status IN $statuses"


class TestFtsStatement:
    """The index DDL, which has to be repeatable and version-consistent."""

    def test_it_does_not_use_the_documented_if_not_exists_clause(self) -> None:
        """The engine's FTS docs show `[IF NOT EXISTS]`; its grammar rejects it.

        ``Parser exception: Invalid input <NOT>``, discovered by running it. The
        first version of this code trusted the documentation, and because the
        resulting failure was swallowed the index silently never existed.
        Repeatability is `ensure_schema`'s job, and it gets it by asking the
        catalogue rather than by re-running a statement the parser refuses.
        """
        assert "IF NOT EXISTS" not in fts_index_statement({})
        assert fts_index_statement({}).startswith("CREATE INDEX experience_fts ")

    def test_it_uses_the_jieba_tokenizer(self) -> None:
        statement = fts_index_statement({})
        assert "tokenizer = 'jieba'" in statement
        assert "jieba_mode = 'mix'" in statement

    def test_it_indexes_every_declared_property(self) -> None:
        statement = fts_index_statement({})
        for name in EXPERIENCE_INDEXED_PROPERTIES:
            assert name in statement

    def test_no_user_dictionary_unless_one_is_configured(self) -> None:
        assert "jieba_dict" not in fts_index_statement({})
        assert "jieba_dict" not in fts_index_statement({"AER_NEUG_JIEBA_DICT": "  "})

    def test_a_configured_dictionary_is_quoted(self) -> None:
        statement = fts_index_statement({"AER_NEUG_JIEBA_DICT": "/etc/aer/dict.utf8"})
        assert "jieba_dict = '/etc/aer/dict.utf8'" in statement

    def test_a_path_containing_a_quote_cannot_break_the_statement(self) -> None:
        """An environment variable is not a licence to concatenate arbitrary text."""
        statement = fts_index_statement({"AER_NEUG_JIEBA_DICT": "/tmp/it's here"})
        assert "\\'" in statement
        assert statement.count("jieba_dict") == 1

    def test_the_index_name_is_the_one_the_metadata_records(self) -> None:
        assert FTS_INDEX_NAME == "experience_fts"
        assert FTS_INDEX_NAME in fts_index_statement({})


class TestSchemaStatements:
    def test_every_statement_is_idempotent(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            assert "IF NOT EXISTS" in statement, statement

    def test_long_text_gets_a_declared_width(self) -> None:
        """``STRING`` is ``VARCHAR(256)``; truncating a problem statement silently
        is worse than refusing the write."""
        experience = SCHEMA_STATEMENTS[0]
        for column in ("problem", "root_cause", "solution", "failed_attempts_text", "avoid_text"):
            assert f"{column} VARCHAR(65535)" in experience, column

    def test_the_two_relationships_declare_their_endpoints(self) -> None:
        joined = "\n".join(SCHEMA_STATEMENTS)
        assert "FROM Experience TO RunRef" in joined
        assert "FROM Experience TO Domain" in joined


class TestEngineAvailability:
    """The failure mode a Windows checkout actually hits."""

    def test_a_missing_engine_is_reported_with_a_way_forward(self) -> None:
        if importlib.util.find_spec("neug") is not None:
            pytest.skip("the engine is installed here; the missing case cannot be simulated")

        with pytest.raises(KnowledgeIndexUnavailable) as excinfo:
            _import_neug()
        message = str(excinfo.value)
        assert "not importable" in message
        assert "retrieval is unavailable" in message

    def test_the_error_mentions_that_only_retrieval_is_affected(self) -> None:
        """An operator who sees this should not think the store is broken."""
        if importlib.util.find_spec("neug") is not None:
            pytest.skip("the engine is installed here")
        with pytest.raises(KnowledgeIndexUnavailable, match="run trace, verification"):
            _import_neug()


class TestEnsureSchemaControlFlow:
    """The ordering of `ensure_schema`, tested without an engine.

    Three consecutive CI failures came out of this method, and two of them were
    sequencing mistakes rather than engine mistakes: a documented clause the parser
    rejects, and a swallowed failure that let the method return an index it had never
    built. Neither needed an engine to catch -- only a test that watched which calls
    were made. These are those tests.
    """

    @staticmethod
    def make_index(
        tmp_path: Path,
        *,
        recorded: int | None,
        ready: list[bool],
        experiences: int = 0,
    ) -> tuple[NeuGKnowledgeIndex, list[str], list[int]]:
        """An index whose engine-facing steps are replaced by recordings.

        ``ready`` is consumed one entry per ``_full_text_ready()`` call, so a test can
        spell out "not there, then there" the way the real sequence goes.
        """
        opened = NeuGKnowledgeIndex(tmp_path / "aer-knowledge")
        executed: list[str] = []
        written: list[int] = []
        queue = list(ready)

        replacements = {
            "projection_version": lambda: recorded,
            "_connect": lambda: object(),
            "_experience_count": lambda: experiences,
            "_execute": lambda statement, parameters=None: executed.append(statement),
            "_write_metadata": lambda: written.append(1),
            "_full_text_ready": lambda: queue.pop(0) if queue else False,
        }
        for name, value in replacements.items():
            setattr(opened, name, value)
        return opened, executed, written

    def test_a_healthy_index_executes_nothing_at_all(self, tmp_path: Path) -> None:
        """The steady-state path.

        The engine logs an error for every object that already exists, so re-asserting
        the schema on every open would fill a healthy system's log with errors -- and a
        real error among them would then be invisible.
        """
        opened, executed, written = self.make_index(tmp_path, recorded=1, ready=[True])
        opened.ensure_schema()

        assert executed == []
        assert written == [1]

    def test_a_damaged_index_is_repaired(self, tmp_path: Path) -> None:
        """Recorded version but no working index: rebuild the schema and the index."""
        opened, executed, written = self.make_index(tmp_path, recorded=1, ready=[False, True])
        opened.ensure_schema()

        assert SCHEMA_STATEMENTS[0] in executed
        assert [s for s in executed if "CREATE INDEX" in s], executed
        assert written == [1]

    def test_an_unknown_index_with_no_experiences_is_adopted(self, tmp_path: Path) -> None:
        """Nothing to lose: an empty graph written by an unknown build is just a graph."""
        opened, executed, written = self.make_index(
            tmp_path, recorded=None, ready=[False, True], experiences=0
        )
        opened.ensure_schema()

        assert SCHEMA_STATEMENTS[0] in executed
        assert written == [1]

    def test_an_unversioned_populated_index_is_refused(self, tmp_path: Path) -> None:
        """Nothing may be assumed about a graph whose layout is unknown."""
        opened, _, written = self.make_index(tmp_path, recorded=None, ready=[True], experiences=3)
        with pytest.raises(KnowledgeSchemaError, match="rebuild"):
            opened.ensure_schema()
        assert written == []

    def test_an_index_that_still_cannot_answer_is_fatal(self, tmp_path: Path) -> None:
        """The fix for the swallowing: never hand back what cannot answer a query."""
        opened, _, written = self.make_index(tmp_path, recorded=1, ready=[False, False, False])
        with pytest.raises(ProjectionError, match="not usable"):
            opened.ensure_schema()
        assert written == []

    def test_a_failed_index_creation_is_fatal(self, tmp_path: Path) -> None:
        opened, _, written = self.make_index(tmp_path, recorded=1, ready=[False, False])
        opened._execute = _ExplodingExecute()  # type: ignore[method-assign]
        with pytest.raises(ProjectionError):
            opened.ensure_schema()
        assert written == []

    def test_a_recorded_version_that_is_not_ours_is_refused(self, tmp_path: Path) -> None:
        opened, executed, written = self.make_index(tmp_path, recorded=99, ready=[True])
        with pytest.raises(KnowledgeSchemaError, match="rebuild"):
            opened.ensure_schema()
        assert executed == []
        assert written == []


class TestFtsIndexCreation:
    """The "already exists" tolerance, which is a success rather than a failure."""

    def test_an_existing_index_is_not_reported_as_a_failure(self, tmp_path: Path) -> None:
        """The engine says `Index already exists: experience_fts`, code 1013.

        Treated as success so the repair path can be unconditional: it does not have to
        know in advance which of the objects it is about to create are already there.
        """
        opened = NeuGKnowledgeIndex(tmp_path / "aer-knowledge")
        opened._execute = _RaisingExecute(  # type: ignore[method-assign]
            "Failed to execute query: CREATE INDEX ... Error code: 1013, Error Message: "
            "ERR_ILLEGAL_OPERATION: Execution failed at operator: [CreateIndexOpr], "
            "Index already exists: experience_fts"
        )
        opened._create_fts_index()

    def test_any_other_failure_is_raised(self, tmp_path: Path) -> None:
        opened = NeuGKnowledgeIndex(tmp_path / "aer-knowledge")
        opened._execute = _RaisingExecute(  # type: ignore[method-assign]
            "Error code: 3000, Error Message: Parser exception: Invalid input <NOT>"
        )
        with pytest.raises(ProjectionError, match="Creating the full-text index"):
            opened._create_fts_index()


class _ExplodingExecute:
    """An ``_execute`` that fails, for the "it cannot be created either" path."""

    def __call__(self, statement: str, parameters: object = None) -> None:
        raise ProjectionError("the engine refused the statement")


class _RaisingExecute:
    """An ``_execute`` that raises with a fixed engine message."""

    def __init__(self, message: str) -> None:
        self._message = message

    def __call__(self, statement: str, parameters: object = None) -> None:
        raise ProjectionError(self._message)
