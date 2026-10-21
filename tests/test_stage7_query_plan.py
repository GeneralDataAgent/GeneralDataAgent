"""Relational compiler counterexamples: ties, duplicate joins, grouping, and scope."""

import sqlite3

import pytest

from generaldataagent.contracts.query_plan import SemanticQueryPlan
from generaldataagent.tools.semantic_sql import compile_semantic_plan
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "plan.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE people(id INT,name TEXT,g TEXT,score REAL);"
            "INSERT INTO people VALUES(1,'A','North',10),(2,'B','North',10),(3,'C','South',5);"
            "CREATE TABLE events(person_id INT); INSERT INTO events VALUES(1),(1),(2);"
        )
    return path


def rank_plan(question="top 1 per group"):
    return {
        "operation": "rank",
        "relations": ["people"],
        "entity_keys": [{"relation": "people", "column": "id"}],
        "projections": [{"field": {"relation": "people", "column": "name"}, "name": "name"}],
        "partition_by": [{"relation": "people", "column": "g"}],
        "metric": {"relation": "people", "column": "score"},
        "source_excerpt": question,
    }


def test_rank_compiler_preserves_ties_per_group(database):
    question = "top 1 per group"
    sql, spec = compile_semantic_plan(
        SemanticQueryPlan.model_validate(rank_plan(question)),
        schema={"people": ["id", "name", "g", "score"]},
        question=question,
    )
    assert set(SQLiteReadOnlyExecutor(database).execute(sql).rows) == {("A",), ("B",), ("C",)}
    assert spec.answer.ties == "all" and "LIMIT" not in sql


def population_ratio_plan(question, **updates):
    return {
        "operation": "population_ratio",
        "relations": ["people", "events"],
        "joins": [
            {
                "left": {"relation": "people", "column": "id"},
                "right": {"relation": "events", "column": "person_id"},
            }
        ],
        "entity_keys": [{"relation": "people", "column": "id"}],
        "numerator_filters": [
            {
                "field": {"relation": "people", "column": "id"},
                "operator": "eq",
                "value": 1,
                "source_excerpt": question,
            }
        ],
        "source_excerpt": question,
        "ratio_name": "share",
    } | updates


def test_population_ratios_keep_declared_entity_or_record_grains_and_public_scale(database):
    from generaldataagent.tools.sql_semantics import sql_data_dependencies, validate_column_scope

    question = "What percentage of people with events have id 1? Round to two decimal places."
    schema = {"people": ["id", "name", "g", "score"], "events": ["person_id"]}
    before = database.read_bytes()
    for basis, expected in [("entities", 50.0), ("records", 66.67)]:
        sql, spec = compile_semantic_plan(
            SemanticQueryPlan.model_validate(population_ratio_plan(question, count_basis=basis)),
            schema=schema,
            question=question,
        )
        assert SQLiteReadOnlyExecutor(database).execute(sql).rows == ((expected,),)
        validate_column_scope(database, sql, allowed_relations=set(schema))
        assert sql_data_dependencies(sql, ["share"], allowed_relations=set(schema)) == [True]
        assert spec.answer.minimum == 0 and spec.answer.maximum == 100
        assert "NULLIF" in sql and "ROUND" in sql
    assert database.read_bytes() == before
    question = "What fraction of people with events have id 1?"
    sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(population_ratio_plan(question, multiplier=1)),
        schema=schema,
        question=question,
    )
    assert SQLiteReadOnlyExecutor(database).execute(sql).rows == ((0.5,),)


def test_population_ratio_keeps_the_denominator_population_and_leaves_empty_undefined(database):
    from generaldataagent.tools.semantic_sql import execute_semantic_plan

    question = "What percentage of people have id 1? Only include g North."
    plan = population_ratio_plan(
        question,
        filters=[
            {
                "field": {"relation": "people", "column": "g"},
                "operator": "eq",
                "value": "North",
                "source_excerpt": question,
            }
        ],
    )
    result = execute_semantic_plan(
        database,
        SemanticQueryPlan.model_validate(plan),
        allowed_relations={"people", "events"},
        question=question,
    )
    assert result["rows"] == [[50.0]]
    assert result["business_semantics_verified"] is False
    empty_question = question.replace("North", "East")
    empty = population_ratio_plan(
        empty_question,
        filters=[
            {
                "field": {"relation": "people", "column": "g"},
                "operator": "eq",
                "value": "East",
                "source_excerpt": empty_question,
            }
        ],
    )
    from generaldataagent.tools.sql_semantics import SemanticError

    with pytest.raises(SemanticError, match="undefined_percentage"):
        execute_semantic_plan(
            database,
            SemanticQueryPlan.model_validate(empty),
            allowed_relations={"people", "events"},
            question=empty_question,
        )
    result = execute_semantic_plan(
        database,
        SemanticQueryPlan.model_validate(empty),
        allowed_relations={"people", "events"},
        question=empty_question,
        instructions="Return NULL if the denominator population is empty.",
    )
    assert result["rows"][0][0] is None


def test_population_ratio_rejects_unsupported_grains_scales_and_unsourced_subset_filters(database):
    question = "What percentage of people with events have id 1?"
    schema = {"people": ["id", "name", "g", "score"], "events": ["person_id"]}
    for update in [
        {"numerator_filters": []},
        {"multiplier": 1},
        {"entity_keys": []},
        {"aggregation": "sum"},
        {"partition_by": [{"relation": "people", "column": "g"}]},
        {
            "numerator_filters": [
                {
                    "field": {"relation": "people", "column": "id"},
                    "operator": "eq",
                    "value": 2,
                    "source_excerpt": question,
                }
            ]
        },
    ]:
        with pytest.raises(ValueError):
            compile_semantic_plan(
                SemanticQueryPlan.model_validate(population_ratio_plan(question, **update)),
                schema=schema,
                question=question,
            )
    count_question = "Count people with id 1."
    with pytest.raises(ValueError, match="fraction-of-population"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(population_ratio_plan(count_question)),
            schema=schema,
            question=count_question,
        )
    with pytest.raises(ValueError, match="measure-based"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(population_ratio_plan(question)),
            schema=schema,
            question=question,
            instructions="Use SUM(score)/COUNT(id).",
        )


def test_standalone_plan_execution_cannot_bypass_complete_result_cell_budget(database):
    from generaldataagent.tools.semantic_sql import execute_semantic_plan
    from generaldataagent.tools.sqlite import QueryExecutionError

    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO people VALUES(?,'D','North',10)", [(i,) for i in range(4, 502)])
    question = "top 1 per group"
    plan = rank_plan(question)
    plan["projections"] = [
        {"field": {"relation": "people", "column": "id"}, "name": f"id_{index}"}
        for index in range(20)
    ]
    before = database.read_bytes()
    with pytest.raises(QueryExecutionError, match="cell limit"):
        execute_semantic_plan(
            database,
            SemanticQueryPlan.model_validate(plan),
            allowed_relations={"people"},
            question=question,
            max_rows=10000,
        )
    assert database.read_bytes() == before


def test_entity_and_record_counts_have_distinct_declared_grains(database):
    question = "count people with events"
    plan = {
        "operation": "count",
        "relations": ["people", "events"],
        "joins": [
            {
                "left": {"relation": "people", "column": "id"},
                "right": {"relation": "events", "column": "person_id"},
            }
        ],
        "entity_keys": [{"relation": "people", "column": "id"}],
        "source_excerpt": question,
    }
    schema = {"people": ["id", "name", "g", "score"], "events": ["person_id"]}
    for basis, expected in [("entities", 2), ("records", 3)]:
        sql, spec = compile_semantic_plan(
            SemanticQueryPlan.model_validate(plan | {"count_basis": basis}),
            schema=schema,
            question=question,
        )
        assert SQLiteReadOnlyExecutor(database).execute(sql).rows == ((expected,),)
        from generaldataagent.tools.sql_semantics import inspect_semantics

        assert not inspect_semantics(sql, spec)


def test_unrequested_filters_unknown_fields_and_cartesian_joins_are_rejected(database):
    schema = {"people": ["id", "name", "g", "score"], "events": ["person_id"]}
    question = "top 1 per group"
    base = rank_plan(question)
    for change in [
        {"metric": {"relation": "people", "column": "secret"}},
        {"relations": ["people", "events"]},
        {"ties": "single"},
        {
            "filters": [
                {
                    "field": {"relation": "people", "column": "score"},
                    "operator": "is_not_null",
                    "source_excerpt": question,
                }
            ]
        },
        {
            "filters": [
                {
                    "field": {"relation": "people", "column": "g"},
                    "operator": "eq",
                    "value": "Hidden",
                    "source_excerpt": question,
                }
            ]
        },
    ]:
        with pytest.raises(ValueError):
            compile_semantic_plan(
                SemanticQueryPlan.model_validate(base | change), schema=schema, question=question
            )


def test_rank_join_aggregation_and_explicit_tie_order(database):
    schema = {"people": ["id", "name", "g", "score"], "events": ["person_id"]}
    question = "top 1 by count of events, break ties using lowest id"
    base = rank_plan(question) | {
        "relations": ["people", "events"],
        "joins": [
            {
                "left": {"relation": "people", "column": "id"},
                "right": {"relation": "events", "column": "person_id"},
            }
        ],
        "partition_by": [],
        "aggregation": "count",
        "metric": None,
        "ties": "single",
        "tie_break_by": [
            {
                "field": {"relation": "people", "column": "id"},
                "direction": "asc",
                "source_excerpt": "lowest id",
            }
        ],
    }
    sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(base), schema=schema, question=question
    )
    assert SQLiteReadOnlyExecutor(database).execute(sql).rows == (("A",),)
    with pytest.raises(ValueError, match="field and direction"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(
                base
                | {
                    "tie_break_by": [
                        {
                            "field": {"relation": "people", "column": "score"},
                            "direction": "asc",
                            "source_excerpt": "lowest id",
                        }
                    ]
                }
            ),
            schema=schema,
            question=question,
        )


def test_filter_evidence_is_local_and_sql_literals_are_escaped(database):
    question = "count people named O'Brien; top 1 elsewhere"
    base = {
        "operation": "count",
        "relations": ["people"],
        "entity_keys": [{"relation": "people", "column": "id"}],
        "source_excerpt": question,
        "filters": [
            {
                "field": {"relation": "people", "column": "name"},
                "operator": "eq",
                "value": "O'Brien",
                "source_excerpt": "named O'Brien",
            }
        ],
    }
    schema = {"people": ["id", "name", "g", "score"]}
    sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(base), schema=schema, question=question
    )
    assert "O''Brien" in sql
    assert SQLiteReadOnlyExecutor(database).execute(sql).rows == ((0,),)
    base["filters"][0]["source_excerpt"] = "top 1 elsewhere"
    with pytest.raises(ValueError, match="public evidence"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(base), schema=schema, question=question
        )


@pytest.mark.parametrize(
    "public,value",
    [
        ("score above 0.5", 5),
        ("score above -5", 5),
        ("score above 5.0", 0),
        ("score above 1e-5", 5),
        ("score above 15", 5),
        ("score is untrue", True),
        ("score is falsehood", False),
        ("name is unspecified", ""),
    ],
)
def test_plan_literals_cannot_borrow_fragments_of_public_values(public, value):
    plan = {
        "operation": "count",
        "relations": ["people"],
        "count_basis": "records",
        "source_excerpt": public,
        "filters": [
            {
                "field": {"relation": "people", "column": "score"},
                "operator": "gt",
                "value": value,
                "source_excerpt": public,
            }
        ],
    }
    with pytest.raises(ValueError, match="public evidence"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(plan), schema={"people": ["score"]}, question=public
        )


@pytest.mark.parametrize(
    "public,value",
    [
        ("score above 0.5.", 0.5),
        ("score above -5.", -5),
        ("score above 5.0", 5),
        ("score above 1e-5", 0.00001),
        ("score above .5", 0.5),
        ("score above +5", 5),
        ("score is TRUE", True),
        ('score is ""', ""),
    ],
)
def test_plan_literals_accept_whole_public_numbers_and_explicit_empty_values(public, value):
    plan = {
        "operation": "count",
        "relations": ["people"],
        "count_basis": "records",
        "source_excerpt": public,
        "filters": [
            {
                "field": {"relation": "people", "column": "score"},
                "operator": "eq",
                "value": value,
                "source_excerpt": public,
            }
        ],
    }
    sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(plan), schema={"people": ["score"]}, question=public
    )
    assert '"people"."score"=' in sql


def test_post_rank_filter_does_not_change_the_ranking_population(database):
    question = "top 1 globally; retain group South"
    base = rank_plan(question) | {
        "partition_by": [],
        "filters": [
            {
                "field": {"relation": "people", "column": "g"},
                "operator": "eq",
                "value": "South",
                "source_excerpt": "group South",
            }
        ],
    }
    schema = {"people": ["id", "name", "g", "score"]}
    before_sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(base), schema=schema, question=question
    )
    assert SQLiteReadOnlyExecutor(database).execute(before_sql).rows == (("C",),)
    base["filters"][0]["phase"] = "after_rank"
    after_sql, _ = compile_semantic_plan(
        SemanticQueryPlan.model_validate(base), schema=schema, question=question
    )
    assert SQLiteReadOnlyExecutor(database).execute(after_sql).rows == ()
    with pytest.raises(ValueError, match="after-rank"):
        compile_semantic_plan(
            SemanticQueryPlan.model_validate(base | {"operation": "count"}),
            schema=schema,
            question=question,
        )


def test_plan_execution_uses_physical_scope_and_never_truncates_ties(database):
    from generaldataagent.tools.semantic_sql import execute_semantic_plan
    from generaldataagent.tools.sqlite import QueryExecutionError

    before = database.read_bytes()
    plan = SemanticQueryPlan.model_validate(rank_plan())
    result = execute_semantic_plan(
        database, plan, allowed_relations={"people"}, question="top 1 per group"
    )
    assert result["complete"] and not result["business_semantics_verified"]
    assert result["rows"] == [["A"], ["B"], ["C"]]
    with pytest.raises(QueryExecutionError, match="row limit"):
        execute_semantic_plan(
            database, plan, allowed_relations={"people"}, question="top 1 per group", max_rows=1
        )
    with pytest.raises(ValueError, match="scope"):
        execute_semantic_plan(
            database, plan, allowed_relations={"events"}, question="top 1 per group"
        )
    assert database.read_bytes() == before
