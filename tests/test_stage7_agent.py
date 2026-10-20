"""Exercise the complete opt-in Agent action loop, not only helper functions."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE people(id INT, birthday TEXT, score INT);"
            "INSERT INTO people VALUES (1,'1960-01-01',10),(2,'1970-01-01',10);"
        )
    return path


def run(database, actions, benchmark="bird-mini-dev-sqlite", question="champions", instructions=""):
    iterator = iter(actions)

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(iterator)),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    task = Task(benchmark, "synthetic", question, instructions, "easy", "data")
    return BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=len(actions)).run(
        task, database
    )


def contract(question="champions", instructions="", **answer):
    return {
        "contract": {"question": question, "public_requirements": instructions, "answer": answer}
    }


def test_sql_ties_rejection_then_repair(database):
    result = run(
        database,
        [
            contract(columns=["id"], ties="all"),
            {"final_sql": "SELECT id FROM people ORDER BY score DESC LIMIT 1"},
            {"final_sql": "SELECT id FROM people WHERE score=(SELECT MAX(score) FROM people)"},
        ],
    )
    assert result["failure"] is None
    assert result["trace"][1]["code"] == "semantic_contract"
    assert "LIMIT" not in result["final_sql"]


def test_answer_is_assembled_from_computed_evidence_not_model_claim(database):
    result = run(
        database,
        [
            contract("count?", "@n[value]", columns=["n"], shape="scalar"),
            {"sql": "SELECT COUNT(*) FROM people"},
            {"answers": {"n": 999}, "evidence_steps": [2], "bindings": {"n": [2, ["rows", 0, 0]]}},
        ],
        benchmark="infiagent-dabench",
        question="count?",
        instructions="@n[value]",
    )
    assert result["failure"] is None
    assert result["answers"] == {"n": 2}
    assert result["raw_answers"] == {"n": 999}


def test_schema_error_recovers_without_exposing_values(database):
    result = run(
        database,
        [
            {"sql": {"credential": "do-not-log"}},
            contract(columns=["id"]),
            {"final_sql": "SELECT id FROM people"},
        ],
    )
    assert result["failure"] is None
    assert result["trace"][0]["code"] == "action_schema"
    assert "do-not-log" not in json.dumps(result["trace"])


def test_no_contract_no_execution_and_no_false_completion(database):
    result = run(database, [{"final_sql": "SELECT id FROM people"}])
    assert result["failure"] and result["final_sql"] is None
    assert not any(e["status"] == "ok" for e in result["trace"])


def test_complete_collection_bypasses_preview_without_losing_ids(database):
    with sqlite3.connect(database) as db:
        db.executemany(
            "INSERT INTO people VALUES (?, '2000-01-01', 1)", [(i,) for i in range(3, 503)]
        )
    result = run(
        database,
        [
            contract("ids?", "@ids[value]", columns=["ids"], shape="set"),
            {"collect": {"sql": "SELECT DISTINCT id FROM people ORDER BY id"}},
            {"answers": {}, "evidence_steps": [2], "bindings": {"ids": [2, ["items"]]}},
        ],
        benchmark="infiagent-dabench",
        question="ids?",
        instructions="@ids[value]",
    )
    assert result["failure"] is None
    assert result["answers"]["ids"] == list(range(1, 503))
    assert result["trace"][1]["tool"] == "complete_collection"


def test_collection_limit_and_unknown_relations_are_rejected(database):
    from generaldataagent.tools.sql_guard import SqlGuardError
    from generaldataagent.tools.sql_semantics import SemanticError, collect_complete

    for sql in [
        "SELECT id FROM people LIMIT 1",
        "SELECT id FROM people WHERE id IN (SELECT id FROM people LIMIT 1)",
    ]:
        with pytest.raises(SemanticError, match="LIMIT"):
            collect_complete(database, sql, allowed_relations={"people"})
    with pytest.raises(SqlGuardError):
        collect_complete(database, "SELECT token FROM secret", allowed_relations={"people"})


def test_runtime_binds_public_text_and_precision(database):
    result = run(
        database,
        [
            {"contract": {"answer": {"columns": ["n"], "shape": "scalar"}}},
            {"sql": "SELECT AVG(id) FROM people"},
            {"answers": {}, "evidence_steps": [2], "bindings": {"n": [2, ["rows", 0, 0]]}},
        ],
        benchmark="infiagent-dabench",
        question="mean?",
        instructions="Round the result to two decimal places. @n[value]",
    )
    assert result["failure"] is None
    assert result["answers"] == {"n": "1.50"}
    assert result["task_contract"]["question"] == "mean?"


def test_crosscheck_disagreement_and_second_check_are_rejected(database):
    result = run(
        database,
        [
            contract(columns=["id"]),
            {"crosscheck": {"sql": "SELECT id FROM people", "purpose": "ties"}},
            {"crosscheck": {"sql": "SELECT id FROM people", "purpose": "ties"}},
            {"final_sql": "SELECT id FROM people WHERE id=1"},
            {"final_sql": "SELECT id FROM people"},
        ],
    )
    assert result["failure"] is None
    assert result["trace"][1]["tool"] == "sql_crosscheck"
    assert [e["code"] for e in result["trace"] if e["status"] == "error"] == [
        "semantic_contract",
        "semantic_contract",
    ]


def test_native_tool_calls_use_same_strict_runtime_validation(database):
    class NativeModel:
        def __init__(self):
            self.actions = iter(
                [
                    {"contract": {"answer": {"columns": ["id"]}}},
                    {"final_sql": "SELECT id FROM people"},
                ]
            )

        def bind(self, **kwargs):
            assert (
                kwargs["tool_choice"] == "required"
                or kwargs["tool_choice"]["function"]["name"] == "plan_analysis"
            )
            assert not kwargs["parallel_tool_calls"]
            return self

        def invoke(self, messages):
            action = next(self.actions)
            if "contract" in action:
                name, arguments = "plan_analysis", action["contract"]
            else:
                name, arguments = "submit_sql", {"sql": action["final_sql"]}
            return SimpleNamespace(
                content="",
                tool_calls=[{"name": name, "args": arguments, "id": "call-1"}],
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    task = Task("bird-mini-dev-sqlite", "native", "ids?", "", "easy", "people")
    result = BoundedSQLAgent(NativeModel(), tool_profile="sql-semantic").run(task, database)
    assert result["failure"] is None
    assert result["final_sql"] == "SELECT id FROM people"


def test_atomic_sql_submission_carries_contract_in_one_action(database):
    result = run(
        database,
        [
            {
                "final_sql": "SELECT id FROM people",
                "task_spec": {"answer": {"columns": ["id"]}},
            }
        ],
    )
    assert result["failure"] is None
    assert len(result["trace"]) == 1
    assert result["task_contract"]["question"] == "champions"


def test_date_boundaries_entity_grain_and_empty_denominator(database):
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE events(person_id INT, day TEXT);"
            "INSERT INTO events VALUES(1,'2025-12-31'),(1,'2026-01-01'),"
            "(1,'2026-01-31'),(2,'2026-02-01');"
        )
    sql = (
        "SELECT COUNT(DISTINCT p.id) AS n FROM people p JOIN events e ON p.id=e.person_id "
        "WHERE e.day >= '2026-01-01' AND e.day < '2026-02-01'"
    )
    result = run(
        database,
        [
            {
                "final_sql": sql,
                "task_spec": {
                    "count_basis": "entities",
                    "time_range": "[2026-01-01,2026-02-01)",
                    "answer": {"columns": ["n"], "shape": "scalar"},
                },
            }
        ],
    )
    assert result["failure"] is None
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    assert SQLiteReadOnlyExecutor(database).execute(result["final_sql"]).rows == ((1,),)
    empty_ratio = run(
        database,
        [
            {
                "final_sql": "SELECT 1.0 / NULLIF(COUNT(*),0) AS r FROM people WHERE id<0",
                "task_spec": {
                    "denominator": "eligible people",
                    "answer": {"columns": ["r"], "shape": "scalar", "nullable": False},
                },
            }
        ],
    )
    assert empty_ratio["failure"] and empty_ratio["final_sql"] is None


def test_age_projection_is_computed_instead_of_returning_birthday(database):
    sql = (
        "SELECT CAST(strftime('%Y','2026-10-20') AS INTEGER) - "
        "CAST(strftime('%Y',birthday) AS INTEGER) AS age FROM people ORDER BY birthday ASC"
    )
    result = run(
        database,
        [
            {
                "final_sql": sql,
                "task_spec": {
                    "order_by": "birthday",
                    "order_direction": "asc",
                    "answer": {"columns": ["age"], "minimum": 0.0},
                },
            }
        ],
    )
    assert result["failure"] is None
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    assert SQLiteReadOnlyExecutor(database).execute(result["final_sql"]).rows == ((66,), (56,))
