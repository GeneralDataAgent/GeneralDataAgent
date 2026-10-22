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


def test_complete_lists_keep_all_rows_duplicates_and_original_collection_guards(tmp_path):
    from generaldataagent.tools.sql_semantics import collect_complete
    from generaldataagent.tools.sqlite import QueryExecutionError

    path = tmp_path / "list.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE observations(value INT);")
        db.executemany("INSERT INTO observations VALUES(?)", [(n // 2,) for n in range(302)])
    before = path.read_bytes()
    result = collect_complete(
        path,
        "SELECT value FROM observations ORDER BY value",
        allowed_relations={"observations"},
        collection_kind="list",
    )
    assert result["complete"] and result["total_count"] == 302
    assert result["items"] == [n // 2 for n in range(302)]
    with pytest.raises(SemanticError, match="duplicates"):
        collect_complete(path, "SELECT value FROM observations", allowed_relations={"observations"})
    for query, message in [
        ("SELECT value FROM observations LIMIT 200", "LIMIT"),
        ("SELECT 302 FROM observations", "data_dependent"),
        ("SELECT value,value FROM observations", "one explicit"),
    ]:
        with pytest.raises(SemanticError, match=message):
            collect_complete(
                path, query, allowed_relations={"observations"}, collection_kind="list"
            )
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO observations VALUES(?)", [(1,)] * 9700)
    before = path.read_bytes()
    with pytest.raises(QueryExecutionError, match="row limit"):
        collect_complete(
            path,
            "SELECT value FROM observations",
            allowed_relations={"observations"},
            collection_kind="list",
        )
    assert path.read_bytes() == before


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


def test_public_mentions_prioritize_wide_late_fields_without_inventing_meanings(tmp_path):
    import json

    path = tmp_path / "wide-target.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE wide ("
            + ",".join(f"c{i} TEXT" for i in range(49))
            + ', "arrival_date" TEXT)'
        )
        db.execute(
            "INSERT INTO wide VALUES (" + ",".join("?" for _ in range(50)) + ")",
            ["unrelated " * 30] * 49 + ["2024-03-15"],
        )
    before = path.read_bytes()
    context = semantic_context(
        path,
        allowed_relations={"wide"},
        max_chars=1500,
        public_text="Compare arrival_date across observations.",
    )
    fields = context["tables"]["wide"]["fields"]
    assert fields[0]["name"] == "arrival_date"
    assert fields[0]["enum_preview"] == ["2024-03-15"]
    assert fields[0]["date_encoding"]["candidates"] == ["iso_date"]
    assert fields[0]["date_encoding"]["verified_business_semantics"] is False
    assert context["tables"]["wide"]["omitted_fields"] > 0
    assert len(json.dumps(context)) <= 1500
    assert path.read_bytes() == before


def test_public_mentions_prioritize_late_tables_and_never_expand_relation_scope(tmp_path):
    path = tmp_path / "many-tables.sqlite"
    with sqlite3.connect(path) as db:
        for index in range(14):
            db.execute(f"CREATE TABLE t{index:02} (value TEXT)")
    allowed = {f"t{index:02}" for index in range(13)}
    context = semantic_context(
        path,
        allowed_relations=allowed,
        max_chars=10000,
        public_text="Inspect t12 and forbidden t13.",
    )
    assert next(iter(context["tables"])) == "t12"
    assert "t13" not in context["tables"]
    assert len(context["tables"]) == 12
    assert context["omitted_table_previews"] == 1


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


def test_complete_missing_column_collection_supports_only_same_field_missing_disjunctions(tmp_path):
    from generaldataagent.tools.sql_semantics import collect_complete, sql_data_dependencies

    path = tmp_path / "missing.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE data(author TEXT,title TEXT); "
            "INSERT INTO data VALUES('na','Good'),('Alice','Other');"
        )
    sql = (
        "SELECT DISTINCT 'author' AS name FROM data "
        "WHERE author IS NULL OR TRIM(author) IN ('NaN','na','null','')"
    )
    result = collect_complete(path, sql, allowed_relations={"data"})
    assert result["items"] == ["author"] and result["complete"]
    for predicate in [
        "author IS NULL OR 1=1",
        "author IS NULL OR title IS NULL",
        "author IN (title)",
    ]:
        assert sql_data_dependencies(
            "SELECT 'author' AS name FROM data WHERE " + predicate,
            ["name"],
            allowed_relations={"data"},
        ) == [False]
    assert sql_data_dependencies(
        "SELECT 'author' AS name FROM data WHERE LOWER(TRIM(author))='na'",
        ["name"],
        allowed_relations={"data"},
    ) == [True]


@pytest.mark.parametrize("operation", ["MIN", "MAX"])
@pytest.mark.parametrize(
    "expression", ["{op}(CAST(p.value AS REAL))", "CAST({op}(p.value) AS REAL)"]
)
def test_explicit_native_extremum_preserves_representation(operation, expression):
    from generaldataagent.tools.public_formulas import check_public_extremum_representation

    instructions = f"The requested extremum refers to {operation}(value);"
    schema = {"observations": ["value"]}
    sql = "SELECT " + expression.format(op=operation) + " AS result FROM observations p"
    assert check_public_extremum_representation(sql, instructions, schema) == [
        "public_extremum_representation"
    ]
    assert not check_public_extremum_representation(
        f"SELECT {operation}(p.value) FROM observations p", instructions, schema
    )


@pytest.mark.parametrize(
    ("sql", "instructions", "schema"),
    [
        (
            "SELECT MAX(CAST(value AS REAL)) FROM observations",
            "Find the maximum value.",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(value AS REAL)) FROM observations",
            "Maximum refers to MAX(value). Convert to a number.",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(value AS REAL)) FROM observations",
            "Do not use MAX(value).",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(value AS REAL)) FROM observations",
            "Maximum refers to MAX(value)",
            {"observations": ["value"], "other": ["value"]},
        ),
        (
            "WITH x AS (SELECT value FROM observations) SELECT MAX(CAST(value AS REAL)) FROM x",
            "Maximum refers to MAX(value)",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(other AS REAL)) FROM observations",
            "Maximum refers to MAX(value)",
            {"observations": ["value", "other"]},
        ),
        (
            "SELECT AVG(CAST(value AS REAL)) FROM observations",
            "Maximum refers to MAX(value)",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(value AS REAL)), COUNT(*) FROM observations",
            "Maximum refers to MAX(value)",
            {"observations": ["value"]},
        ),
        (
            "SELECT MAX(CAST(value AS REAL)) FROM observations",
            "Upper refers to MAX(value); lower refers to MIN(value)",
            {"observations": ["value"]},
        ),
    ],
)
def test_native_extremum_guard_abstains_outside_explicit_unambiguous_scope(
    sql, instructions, schema
):
    from generaldataagent.tools.public_formulas import check_public_extremum_representation

    assert not check_public_extremum_representation(sql, instructions, schema)


def test_public_conversion_request_in_question_disables_native_extremum_guard():
    from generaldataagent.tools.public_formulas import check_public_extremum_representation

    assert not check_public_extremum_representation(
        "SELECT MAX(CAST(value AS REAL)) FROM observations",
        "Maximum refers to MAX(value)",
        {"observations": ["value"]},
        question="Convert the maximum to a real number.",
    )


@pytest.mark.parametrize(
    "pattern,issues",
    [
        ("0:02:07%", ["public_time_like_pattern"]),
        ("2:07%", []),
        ("%2:07%", ["public_time_like_pattern"]),
    ],
)
def test_explicit_public_zero_hour_pattern_checks_actual_physical_field(pattern, issues):
    from generaldataagent.tools.public_formulas import check_public_time_pattern

    assert (
        check_public_time_pattern(
            f"SELECT id FROM runs r WHERE r.lap LIKE '{pattern}'",
            "Which id finished 0:02:07?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["id", "lap"]},
        )
        == issues
    )


@pytest.mark.parametrize(
    "question,instructions,schema,sql",
    [
        (
            "Which id finished 0:02:07?",
            "Return the time.",
            {"runs": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '0:02:07%'",
        ),
        (
            "Which id finished 1:02:07?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '1:02:07%'",
        ),
        (
            "Between 0:02:07 and 0:03:08?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '0:02:07%'",
        ),
        (
            "Which id finished 0:62:07?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '0:62:07%'",
        ),
        (
            "Which id finished 0:02:07?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"], "other": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '0:02:07%'",
        ),
        (
            "Which id finished 0:02:07?",
            "finished 0:0M:SS refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"]},
            "WITH x AS (SELECT lap FROM runs) SELECT lap FROM x WHERE lap LIKE '0:02:07%'",
        ),
        (
            "Which id finished 0:02:07?",
            "Do not use 0:0M:SS; lap refers to lap LIKE 'M:SS%'",
            {"runs": ["lap"]},
            "SELECT lap FROM runs WHERE lap LIKE '0:02:07%'",
        ),
    ],
)
def test_public_time_pattern_does_not_invent_mapping_or_resolve_ambiguous_sources(
    question, instructions, schema, sql
):
    from generaldataagent.tools.public_formulas import check_public_time_pattern

    assert not check_public_time_pattern(sql, question, instructions, schema)


@pytest.mark.parametrize(
    "question,instructions,expected",
    [
        (
            "What is the increase rate from A to B?",
            "rate=(B-A)/A*100",
            ["unrequested_change_intermediate_outputs"],
        ),
        ("What is the increase rate and raw difference?", "Report both.", []),
        ("What is the growth rate for each group?", "", []),
        ("What is the change rate?", "@rate[value] @difference[value]", []),
    ],
)
def test_single_rate_projection_does_not_include_unrequested_intermediates(
    question, instructions, expected
):
    from generaldataagent.tools.public_formulas import check_public_rate_projection

    assert (
        check_public_rate_projection(
            "SELECT a-b AS delta, 100.0*(a-b)/b AS rate FROM data", question, instructions
        )
        == expected
    )
    assert not check_public_rate_projection(
        "SELECT 100.0*(a-b)/b AS rate FROM data", question, instructions
    )


@pytest.mark.parametrize("expression", ["ABS(a-b)", "ABS(b-a)"])
def test_public_signed_difference_is_not_changed_to_distance(expression):
    from generaldataagent.tools.public_formulas import check_public_signed_difference

    assert check_public_signed_difference(
        f"SELECT label FROM data WHERE {expression}>30",
        "List labels with difference over 30.",
        "Difference = a-b",
        {"data": ["label", "a", "b"]},
    ) == ["public_signed_difference_changed_to_absolute"]


@pytest.mark.parametrize(
    "sql,question,instructions,schema",
    [
        (
            "SELECT a FROM data WHERE ABS(a-b)>30",
            "List absolute differences.",
            "Difference=a-b",
            {"data": ["a", "b"]},
        ),
        (
            "SELECT a FROM data WHERE ABS(a-b)>30",
            "List differences.",
            "Difference=a-b",
            {"data": ["a", "b"], "other": ["a"]},
        ),
        (
            "WITH d AS(SELECT a,b FROM data) SELECT a FROM d WHERE ABS(a-b)>30",
            "List differences.",
            "Difference=a-b",
            {"data": ["a", "b"]},
        ),
        (
            "SELECT a FROM data WHERE ABS(a-b)>30",
            "List differences.",
            "Difference=a-b; other=c-d",
            {"data": ["a", "b", "c", "d"]},
        ),
        (
            "SELECT a FROM data WHERE ABS(c-d)>30",
            "List differences.",
            "Difference=a-b",
            {"data": ["a", "b", "c", "d"]},
        ),
        (
            "SELECT a FROM data WHERE a-b>30",
            "List differences.",
            "Difference=a-b",
            {"data": ["a", "b"]},
        ),
    ],
)
def test_signed_difference_guard_abstains_on_unrelated_or_ambiguous_expression(
    sql, question, instructions, schema
):
    from generaldataagent.tools.public_formulas import check_public_signed_difference

    assert not check_public_signed_difference(sql, question, instructions, schema)


def test_missing_string_check_is_per_physical_union_branch_and_column():
    from generaldataagent.tools.public_formulas import check_public_missing_nulls

    text = "Which columns contain missing values? Only object strings; include an empty string."
    schema = {"data": ["author", "title"]}
    bad = (
        "SELECT DISTINCT 'author' AS name FROM data WHERE TRIM(author) IN ('na','') "
        "UNION SELECT DISTINCT 'title' FROM data WHERE title IS NULL OR TRIM(title) IN "
        "('na','')"
    )
    assert check_public_missing_nulls(bad, text, schema) == [
        "missing_string_branch_omits_imported_nulls"
    ]
    good = bad.replace("WHERE TRIM(author)", "WHERE author IS NULL OR TRIM(author)")
    assert check_public_missing_nulls(good, text, schema) == []
    for sql in [
        "SELECT 'author' FROM data d WHERE d.title IS NULL OR TRIM(d.author) IN ('')",
        "SELECT 'author' FROM data WHERE NOT author IS NULL OR TRIM(author) IN ('')",
    ]:
        assert check_public_missing_nulls(sql, text, schema)
    for sql in [
        "SELECT 'author' FROM data WHERE COALESCE(author,'') IN ('')",
        "WITH x AS (SELECT author FROM data) SELECT 'author' FROM x WHERE TRIM(author) IN ('')",
        "SELECT 'author' FROM data WHERE author IS NULL",
        "SELECT 'author' FROM data WHERE TRIM(author) IN ('na')",
        "SELECT 'author' FROM data WHERE NOT (TRIM(author) IN (''))",
    ]:
        assert check_public_missing_nulls(sql, text, schema) == []
    assert (
        check_public_missing_nulls(bad, "Only exact literal values; exclude SQL NULL.", schema)
        == []
    )
