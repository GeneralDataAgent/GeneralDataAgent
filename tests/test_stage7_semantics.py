"""Synthetic counterexamples independent of benchmark reference answers."""

import sqlite3

import pytest

from generaldataagent.catalog.semantic_context import semantic_context
from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec
from generaldataagent.tools.sql_semantics import (
    SemanticError,
    assemble_answers,
    inspect_semantics,
    validate_rows,
)


def test_ties_direction_projection_and_entity_grain():
    spec = AnalysisTaskSpec(
        question="all oldest people",
        order_by="birthday",
        order_direction="asc",
        count_basis="entities",
        answer=AnswerSpec(columns=["id"], ties="all"),
    )
    issues = inspect_semantics("SELECT birthday FROM people ORDER BY birthday DESC LIMIT 1", spec)
    assert set(issues) == {"output_projection", "ties_may_be_truncated", "order_direction"}
    assert not inspect_semantics("SELECT id FROM people ORDER BY birthday ASC", spec)
    assert "joined_entity_count_requires_distinct" in inspect_semantics(
        "SELECT COUNT(*) FROM people JOIN events ON people.id=events.id", spec
    )


def test_denominator_and_post_execution_cardinality():
    spec = AnalysisTaskSpec(question="ratio", denominator="eligible records")
    assert "zero_denominator_unprotected" in inspect_semantics("SELECT 1 / COUNT(*) FROM x", spec)
    assert not inspect_semantics("SELECT 1.0 / NULLIF(COUNT(*), 0) FROM x", spec)
    assert validate_rows(["id"], [], AnswerSpec(empty="reject")) == ["empty_result"]
    assert "duplicate_entities" in validate_rows(["id"], [(1,), (1,)], AnswerSpec(shape="set"))
    assert "incomplete_result" in validate_rows(["id"], [(1,)], AnswerSpec(), complete=False)
    assert "scalar_cardinality" in validate_rows(["x"], [], AnswerSpec(shape="scalar"))


def test_answer_assembly_uses_full_result_and_explicit_rounding_only():
    evidence = {1: {"rows": [(0.252407,)]}}
    bindings = {"r": (1, ["rows", 0, 0])}
    assert assemble_answers(bindings, evidence, AnswerSpec(columns=["r"])) == {"r": 0.252407}
    assert assemble_answers(bindings, evidence, AnswerSpec(columns=["r"], decimals=3)) == {
        "r": "0.252"
    }
    assert evidence[1]["rows"][0][0] == 0.252407
    with pytest.raises(SemanticError):
        assemble_answers({"r": (2, ["rows", 0, 0])}, evidence, AnswerSpec())
    with pytest.raises(SemanticError):
        assemble_answers({"r": (1, ["rows", -1, 0])}, evidence, AnswerSpec())


def test_scoped_context_and_observed_many_to_many(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE p(k INT); CREATE TABLE c(k INT REFERENCES p(k));"
            "CREATE TABLE secret(token TEXT); INSERT INTO p VALUES(1),(1);"
            "INSERT INTO c VALUES(1),(1),(2);"
        )
    before = path.read_bytes()
    result = semantic_context(path, allowed_relations={"p", "c"})
    assert set(result["tables"]) == {"p", "c"}
    assert result["cardinalities"][0]["many_to_many_risk"]
    assert result["tables"]["c"]["row_count"] == 3
    assert before == path.read_bytes()


def test_semantic_context_has_a_global_serialized_budget(tmp_path):
    import json

    path = tmp_path / "wide.sqlite"
    with sqlite3.connect(path) as db:
        columns = ",".join(f"c{i} TEXT" for i in range(50))
        db.execute(f"CREATE TABLE wide ({columns})")
        db.execute(
            "INSERT INTO wide VALUES (" + ",".join("?" for _ in range(50)) + ")",
            ["long sample " * 100] * 50,
        )
    context = semantic_context(path, allowed_relations={"wide"}, max_chars=1500)
    assert len(json.dumps(context)) <= 1500
    assert context["tables"]["wide"]["omitted_fields"] > 0


def test_scope_resolution_rejects_sqlite_double_quoted_string_typos(tmp_path):
    from generaldataagent.tools.sql_semantics import validate_column_scope

    path = tmp_path / "scope.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE people(id INT, "Birth Date" TEXT);'
            "INSERT INTO people VALUES(1,'2000-01-01');"
        )
    before = path.read_bytes()
    for sql in ['SELECT "misspelled" FROM people', "SELECT absent FROM people"]:
        with pytest.raises(SemanticError, match="scoped SQL column"):
            validate_column_scope(path, sql, allowed_relations={"people"})
    for sql in [
        "WITH p AS (SELECT id FROM people) SELECT id FROM p",
        "SELECT id AS identifier FROM people ORDER BY identifier",
        'SELECT "Birth Date" FROM people',
        "SELECT COUNT(*) FROM people",
    ]:
        validate_column_scope(path, sql, allowed_relations={"people"})
    assert path.read_bytes() == before


def test_join_profile_counts_actual_matches_and_excludes_null_or_unmatched_duplicates(tmp_path):
    path = tmp_path / "cardinality.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE p(k INT); CREATE TABLE c(k INT REFERENCES p(k));"
            "INSERT INTO p VALUES(1),(1),(NULL),(NULL),(9),(9);"
            "INSERT INTO c VALUES(1),(1),(2),(2),(NULL),(NULL);"
        )
    result = semantic_context(
        path, allowed_relations={"p", "c"}, public_text="identifier means c.k"
    )
    observed = result["cardinalities"][0]
    assert observed["inner_join_rows"] == 4
    assert observed["matched_left_rows"] == 2
    assert observed["expansion_per_matched_left"] == 2
    assert result["public_field_mappings"]["mappings"][0]["table"] == "c"


def test_column_repair_candidates_are_physical_and_scoped(tmp_path):
    from generaldataagent.tools.sql_semantics import ColumnScopeError, validate_column_scope

    path = tmp_path / "suggest.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE visible(birthday TEXT); CREATE TABLE hidden(birthdate_secret TEXT);"
        )
    with pytest.raises(ColumnScopeError) as caught:
        validate_column_scope(path, "SELECT birthda FROM visible", allowed_relations={"visible"})
    assert caught.value.candidates == ["birthday"]
    assert "secret" not in str(caught.value.candidates)


def test_entity_count_checks_keep_join_and_aggregate_in_the_same_scope():
    spec = AnalysisTaskSpec(question="count unique people", count_basis="entities")
    valid = [
        "SELECT COUNT(*) FROM (SELECT DISTINCT p.id FROM people p JOIN events e ON p.id=e.id)",
        "WITH ids AS (SELECT DISTINCT p.id FROM people p JOIN events e ON p.id=e.id) "
        "SELECT COUNT(*) FROM ids",
        "SELECT COUNT(DISTINCT p.id) FROM people p JOIN events e ON p.id=e.id",
        "SELECT (SELECT COUNT(*) FROM events) FROM people p JOIN departments d ON p.dept=d.id",
    ]
    for sql in valid:
        assert "joined_entity_count_requires_distinct" not in inspect_semantics(sql, spec)
    invalid = (
        "WITH n AS (SELECT COUNT(*) AS c FROM people p JOIN events e ON p.id=e.id) SELECT c FROM n"
    )
    assert "joined_entity_count_requires_distinct" in inspect_semantics(invalid, spec)


def test_data_dependencies_distinguish_computations_counts_and_constant_wrappers():
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    cases = [
        ("SELECT AVG(id) AS x FROM people", True),
        ("SELECT COUNT(*) AS x FROM people", True),
        ("WITH a AS (SELECT id FROM people) SELECT COUNT(*) AS x FROM a", True),
        ("SELECT CASE WHEN COUNT(*)>1 THEN 'many' ELSE 'few' END AS x FROM people", True),
        ("SELECT 0.99 AS x FROM people", False),
        ("WITH a AS (SELECT 0.99 AS x FROM people) SELECT x FROM a", False),
        (
            "WITH unused AS (SELECT id FROM people), a AS (SELECT 7 AS x) "
            "SELECT COUNT(*) AS x FROM a",
            False,
        ),
    ]
    for sql, expected in cases:
        assert sql_data_dependencies(sql, ["x"], allowed_relations={"people"}) == [expected]


def test_constant_complete_collection_does_not_certify_model_selected_ids(tmp_path):
    from generaldataagent.tools.sql_semantics import collect_complete

    path = tmp_path / "constant.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE ids(id INT); INSERT INTO ids VALUES(1),(2);")
    with pytest.raises(SemanticError, match="data_dependent_answer_required"):
        collect_complete(path, "SELECT DISTINCT 999 AS id FROM ids", allowed_relations={"ids"})


def test_percentage_bounds_do_not_constrain_growth_or_explicit_null_policy():
    from generaldataagent.tools.sql_semantics import check_percentage_result

    sql = "SELECT 100.0*SUM(matches)/NULLIF(COUNT(*),0) AS pct FROM records"
    assert check_percentage_result(sql, "percentage of eligible records?", "", [(None,)]) == [
        "undefined_percentage_check_population"
    ]
    assert check_percentage_result(sql, "percentage of eligible records?", "", [(101,)]) == [
        "percentage_out_of_range"
    ]
    assert not check_percentage_result(sql, "percentage of eligible records?", "", [(100,)])
    assert not check_percentage_result(sql, "percentage increase of records?", "", [(200,)])
    assert not check_percentage_result(
        sql, "percentage of eligible records?", "Return NULL if no records.", [(None,)]
    )


def test_percentage_population_guard_does_not_bound_arbitrary_weighted_ratios():
    from generaldataagent.tools.sql_semantics import check_percentage_result

    question = "percentage of cash flow?"
    assert not check_percentage_result(
        "SELECT SUM(inflow)*100.0/SUM(net_flow) FROM accounts", question, "", [(200,)]
    )
    assert check_percentage_result(
        "SELECT SUM(eligible)*100.0/SUM(CASE WHEN active THEN 1 ELSE 0 END) FROM accounts",
        "percentage of accounts?",
        "",
        [(None,)],
    ) == ["undefined_percentage_check_population"]


def test_count_lineage_uses_its_own_scope_and_supports_count_one():
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    examples = [
        ("SELECT COUNT(1) AS x FROM people", True),
        ("SELECT COUNT(NULL) AS x FROM people", False),
        ("SELECT (SELECT COUNT(*) FROM (SELECT 1 AS n) a) AS x FROM people", False),
        ("WITH a AS (SELECT id FROM people) SELECT (SELECT COUNT(*) FROM a) AS x", True),
    ]
    for sql, expected in examples:
        assert sql_data_dependencies(sql, ["x"], allowed_relations={"people"}) == [expected]


def test_physical_column_spaces_and_dots_are_not_normalized_into_sqlite_constants(tmp_path):
    from generaldataagent.tools.sql_semantics import ColumnScopeError, validate_column_scope
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    path = tmp_path / "literal-schema.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE data(" BARO" REAL,"trailing " REAL,"dotted.name" REAL);'
            "INSERT INTO data VALUES(10,20,30);"
            'CREATE TABLE hidden("BARO_private" REAL);'
        )
    before = path.read_bytes()
    for sql, expected in [
        ('SELECT "BARO" FROM data', " BARO"),
        ('SELECT "trailing" FROM data', "trailing "),
    ]:
        with pytest.raises(ColumnScopeError) as error:
            validate_column_scope(path, sql, allowed_relations={"data"})
        assert expected in error.value.candidates
        assert not any("private" in name for name in error.value.candidates)
    sql = 'SELECT " BARO", "trailing ", "dotted.name" FROM data'
    validate_column_scope(path, sql, allowed_relations={"data"})
    assert SQLiteReadOnlyExecutor(path).execute(sql).rows == ((10, 20, 30),)
    assert path.read_bytes() == before


def test_sqlite_rowid_aliases_need_real_table_support(tmp_path):
    from generaldataagent.tools.sql_semantics import ColumnScopeError, validate_column_scope
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    path = tmp_path / "rowid.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE ordinary(value INT); INSERT INTO ordinary VALUES(7);"
            "CREATE TABLE stable(id INT PRIMARY KEY) WITHOUT ROWID; INSERT INTO stable VALUES(9);"
        )
    sql = "SELECT rowid, _rowid_, oid FROM ordinary"
    validate_column_scope(path, sql, allowed_relations={"ordinary"})
    assert SQLiteReadOnlyExecutor(path).execute(sql).rows == ((1, 1, 1),)
    for alias in ("rowid", "_rowid_", "oid"):
        with pytest.raises(ColumnScopeError):
            validate_column_scope(
                path, f'SELECT "{alias}" FROM stable', allowed_relations={"stable"}
            )


def test_sqlite_non_ascii_column_case_is_not_silently_folded(tmp_path):
    from generaldataagent.tools.sql_semantics import ColumnScopeError, validate_column_scope
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    path = tmp_path / "unicode.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE data("Σ" INT,"σ" INT,"Ω" INT,"Mixed" INT);'
            "INSERT INTO data VALUES(10,20,30,40);"
        )
    sql = 'SELECT "Σ","σ","Ω","MIXED" FROM data'
    validate_column_scope(path, sql, allowed_relations={"data"})
    assert SQLiteReadOnlyExecutor(path).execute(sql).rows == ((10, 20, 30, 40),)
    with pytest.raises(ColumnScopeError):
        validate_column_scope(path, 'SELECT "ω" FROM data', allowed_relations={"data"})


@pytest.mark.parametrize(
    "predicate",
    [
        "name='Winter'",
        "active IS NOT NULL",
        "score > 5 AND active=1",
        "NOT active",
        "score BETWEEN 1 AND 5",
        "name IN ('A','B')",
        "name LIKE 'A%'",
    ],
)
def test_percentage_population_counts_sqlite_boolean_predicates(predicate):
    from generaldataagent.tools.sql_semantics import check_percentage_result

    sql = f"SELECT 100.0*SUM(eligible)/SUM({predicate}) AS pct FROM records"
    assert check_percentage_result(sql, "percentage of eligible records?", "", [(None,)]) == [
        "undefined_percentage_check_population"
    ]
    assert check_percentage_result(sql, "percentage of eligible records?", "", [(105,)]) == [
        "percentage_out_of_range"
    ]
    assert not check_percentage_result(sql, "percentage of eligible records?", "", [(50,)])
    assert not check_percentage_result(sql, "percentage growth of records?", "", [(105,)])


def test_percentage_projection_check_leaves_precomputed_or_averaged_rates_unclassified():
    from generaldataagent.tools.sql_semantics import check_percentage_result

    question = "What is the percentage of eligible records?"
    for sql in [
        "SELECT pct FROM stored_rates",
        "SELECT AVG(eligible)*100 AS pct FROM records",
        "SELECT SUM(weighted_rate) AS pct FROM records",
    ]:
        assert not check_percentage_result(sql, question, "", [(50,)])
    assert not check_percentage_result(
        "SELECT COUNT(*) FROM records", "How many records?", "", [(5,)]
    )


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT id AS x FROM "Ä"',
        'SELECT COUNT(*) AS x FROM "Ä"',
        'WITH "ß" AS (SELECT id FROM "Ä") SELECT id AS x FROM "ß"',
    ],
)
def test_sql_data_dependency_preserves_unicode_physical_relation_identity(sql):
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    assert sql_data_dependencies(sql, ["x"], allowed_relations={"Ä"}) == [True]
    assert sql_data_dependencies(sql, ["x"], allowed_relations={"ä"}) == [False]
    assert sql_data_dependencies('SELECT id FROM "a.b"', ["id"], allowed_relations={"a.b"}) == [
        True
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 999 AS x UNION ALL SELECT id AS x FROM people",
        "WITH c AS (SELECT 999 AS x UNION ALL SELECT id AS x FROM people) SELECT x FROM c",
        "SELECT x FROM (SELECT 999 AS x UNION ALL SELECT id AS x FROM people) q",
        "SELECT 'author' AS x UNION ALL SELECT name AS x FROM people",
    ],
)
def test_union_data_branch_cannot_certify_another_branch_constants(sql):
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    assert sql_data_dependencies(sql, ["x"], allowed_relations={"people"}) == [False]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id AS x FROM people UNION ALL SELECT id AS x FROM people",
        "SELECT id+(SELECT 1) AS x FROM people",
        "WITH c AS(SELECT 1 AS n) SELECT id+n AS x FROM people CROSS JOIN c",
        "SELECT DISTINCT 'author' AS x FROM people WHERE author IS NULL",
        "SELECT 'author' AS x WHERE (SELECT COUNT(*) FROM people WHERE author IS NULL)>0",
    ],
)
def test_union_guard_preserves_computed_values_and_data_conditional_schema_labels(sql):
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    assert sql_data_dependencies(sql, ["x"], allowed_relations={"people"}) == [True]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'author' AS x FROM people",
        "SELECT 'author' AS x FROM people WHERE title IS NULL",
        "SELECT 'invented' AS x WHERE (SELECT COUNT(*) FROM people WHERE author IS NULL)>0",
        "SELECT 'author' AS x WHERE (SELECT COUNT(*) FROM people WHERE author IS NULL)>=0",
        "SELECT 'author' AS x WHERE (SELECT COUNT(*) FROM people WHERE author IS NULL)>0 OR 1=1",
        "WITH c AS (SELECT NULL AS author FROM people) "
        "SELECT 'author' AS x FROM c WHERE author IS NULL",
    ],
)
def test_conditional_label_exception_requires_actual_gated_physical_field(sql):
    from generaldataagent.tools.sql_semantics import sql_data_dependencies

    assert sql_data_dependencies(sql, ["x"], allowed_relations={"people"}) == [False]


def test_complete_column_name_collection_is_conditional_and_union_cannot_add_guessed_ids(tmp_path):
    from generaldataagent.tools.sql_semantics import collect_complete

    path = tmp_path / "collection.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE people(id INT,author TEXT,title TEXT); "
            "INSERT INTO people VALUES(1,NULL,'Hello'),(2,'A','World');"
        )
    before = path.read_bytes()
    sql = (
        "SELECT DISTINCT name FROM ("
        "SELECT 'author' AS name WHERE (SELECT COUNT(*) FROM people WHERE author IS NULL)>0 "
        "UNION ALL SELECT 'title' WHERE (SELECT COUNT(*) FROM people WHERE title IS NULL)>0) q "
        "ORDER BY name"
    )
    result = collect_complete(path, sql, allowed_relations={"people"})
    assert result == {"items": ["author"], "total_count": 1, "complete": True}
    with pytest.raises(SemanticError, match="data_dependent_answer_required"):
        collect_complete(
            path,
            "SELECT DISTINCT id FROM (SELECT 999 AS id UNION ALL SELECT id FROM people) q",
            allowed_relations={"people"},
        )
    assert path.read_bytes() == before
