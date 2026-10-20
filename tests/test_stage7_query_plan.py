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
