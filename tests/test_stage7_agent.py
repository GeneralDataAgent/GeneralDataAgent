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
            assert kwargs["tool_choice"] == "required" or kwargs["tool_choice"]["function"][
                "name"
            ] in {"plan_analysis", "submit_sql"}
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


def test_rejected_inline_contract_does_not_lock_future_repair(database):
    result = run(
        database,
        [
            {"final_sql": "SELECT id FROM people", "task_spec": {"answer": {"columns": ["wrong"]}}},
            {"final_sql": "SELECT id FROM people", "task_spec": {"answer": {"columns": ["id"]}}},
        ],
    )
    assert result["failure"] is None
    assert result["task_contract"]["answer"]["columns"] == ["id"]
    assert result["trace"][0]["code"] == "semantic_contract"


def test_native_transcript_pairs_every_rejected_call_with_a_result():
    from langchain_core.messages import AIMessage, ToolMessage

    from generaldataagent.evaluation.stage_seven import ToolConversation

    transcript = ToolConversation([("system", "read only")])
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_sql", "args": {"sql": "SELECT 1"}, "id": "one"},
            {"name": "execute_sql", "args": {"sql": "SELECT 2"}, "id": "two"},
        ],
    )
    transcript.record_calls(response)
    transcript.append(("human", '{"error":"one action required"}'))
    assert isinstance(transcript[1], AIMessage)
    assert all(isinstance(message, ToolMessage) for message in transcript[2:])
    assert [message.tool_call_id for message in transcript[2:]] == ["one", "two"]
    assert not transcript.pending


def test_public_predicate_requirement_is_enforced_before_execution(database):
    spec = {
        "answer": {"columns": ["id"]},
        "requirements": {"predicates": [{"expression": "id=1", "source_excerpt": "id 1"}]},
    }
    result = run(
        database,
        [
            {"final_sql": "SELECT id FROM people WHERE id=1 OR score=10", "task_spec": spec},
            {"final_sql": "SELECT id FROM people WHERE id=1", "task_spec": spec},
        ],
        question="return id 1",
    )
    assert result["failure"] is None
    assert result["trace"][0]["code"] == "semantic_contract"
    assert len([e for e in result["trace"] if e["status"] == "ok"]) == 1


def test_native_statistics_use_two_actions_and_keep_full_precision_receipts(database):
    from langchain_core.messages import AIMessage, ToolMessage

    class Native:
        def __init__(self):
            self.actions = iter(
                [
                    (
                        "fixed_analysis",
                        {"table": "people", "columns": ["score"], "operation": "describe"},
                    ),
                    (
                        "submit_answers",
                        {
                            "fields": [
                                {"name": "mean", "step": 1, "path": ["statistics", "score", "mean"]}
                            ]
                        },
                    ),
                ]
            )
            self.calls = 0

        def bind(self, **kwargs):
            assert {t["function"]["name"] for t in kwargs["tools"]} == {
                "compute_sql",
                "fixed_analysis",
                "search_documents",
                "collect_values",
                "submit_answers",
                "declare_unsupported",
            }
            return self

        def invoke(self, messages):
            if self.calls:
                assert any(isinstance(message, ToolMessage) for message in messages)
            self.calls += 1
            name, args = next(self.actions)
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(self.calls)}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    model = Native()
    task = Task(
        "infiagent-dabench",
        "synthetic",
        "mean?",
        "Round the result to two decimal places. @mean[value]",
        "easy",
        "people",
    )
    result = BoundedSQLAgent(model, tool_profile="sql-semantic").run(task, database)
    assert result["failure"] is None and model.calls == 2
    assert result["answers"] == {"mean": "10.00"}
    assert result["answer_evidence"]["mean"]["raw_value"] == 10
    assert result["answer_evidence"]["mean"]["method_id"] == "describe"


def test_public_float_definition_is_rounded_without_a_model_precision_declaration(database):
    result = run(
        database,
        [
            {"sql": "SELECT AVG(id) AS mean FROM people"},
            {"submit_answers": {"fields": [{"name": "mean", "step": 1, "path": ["rows", 0, 0]}]}},
        ],
        benchmark="infiagent-dabench",
        question="mean?",
        instructions='@mean[number] where "number" is a float with two decimal places.',
    )
    assert result["failure"] is None
    assert result["answers"] == {"mean": "1.50"}


def test_runtime_applies_mixed_public_field_rules_without_model_restatement(database):
    instructions = (
        "@mean[average] @total[sum_value] @label[category]\n"
        'where "average" is a number rounded to two decimal places.\n'
        '"sum_value" is a number rounded to four decimal places.\n'
        '"category" is a string.'
    )
    result = run(
        database,
        [
            {
                "sql": "SELECT AVG(id), SUM(id), "
                "CASE WHEN COUNT(*)>0 THEN 'observed' ELSE 'empty' END FROM people"
            },
            {
                "submit_answers": {
                    "fields": [
                        {"name": name, "step": 1, "path": ["rows", 0, i]}
                        for i, name in enumerate(["mean", "total", "label"])
                    ]
                }
            },
        ],
        benchmark="infiagent-dabench",
        question="mean, total and label?",
        instructions=instructions,
    )
    assert result["failure"] is None
    assert result["answers"] == {"mean": "1.50", "total": "3.0000", "label": "observed"}
    assert result["task_contract"]["answer"]["fields"]["label"]["kind"] == "string"
    assert result["answer_evidence"]["total"]["raw_value"] == 3


def test_public_string_contract_rejects_numeric_evidence_without_model_rule(database):
    result = run(
        database,
        [
            {"sql": "SELECT id FROM people ORDER BY id LIMIT 1"},
            {"submit_answers": {"fields": [{"name": "label", "step": 1, "path": ["rows", 0, 0]}]}},
        ],
        benchmark="infiagent-dabench",
        question="label?",
        instructions='@label[category] where "category" is a string.',
    )
    assert result["failure"] == "execution_or_model_error"
    assert not result["answers"]


def test_model_constants_cannot_be_laundered_through_successful_sql(database):
    result = run(
        database,
        [
            {"sql": "WITH fabricated AS (SELECT 0.99 AS estimate) SELECT estimate FROM fabricated"},
            {"submit_answers": {"fields": [{"name": "mean", "step": 1, "path": ["rows", 0, 0]}]}},
            {"sql": "SELECT AVG(id) AS mean FROM people"},
            {"submit_answers": {"fields": [{"name": "mean", "step": 3, "path": ["rows", 0, 0]}]}},
        ],
        benchmark="infiagent-dabench",
        question="mean?",
        instructions="@mean[value]",
    )
    assert result["failure"] is None
    assert result["trace"][1]["checks"] == ["data_dependent_answer_required"]
    assert result["answers"] == {"mean": 1.5}


def test_constant_answer_cannot_bypass_dependency_check_via_crosscheck(database):
    result = run(
        database,
        [
            {"crosscheck": {"sql": "SELECT 0.99 AS x", "purpose": "output_range"}},
            {"submit_answers": {"fields": [{"name": "n", "step": 1, "path": ["rows", 0, 0]}]}},
        ],
        benchmark="infiagent-dabench",
        question="count?",
        instructions="@n[value]",
    )
    assert result["failure"] == "execution_or_model_error"
    assert result["trace"][1]["checks"] == ["data_dependent_answer_required"]


def test_undefined_percentage_is_replanned_instead_of_submitted_as_success(database):
    question = "What is the percentage of people scoring 10?"
    spec = {"answer": {"columns": ["pct"], "shape": "scalar"}}
    result = run(
        database,
        [
            {
                "final_sql": "SELECT 100.0*SUM(score=10)/COUNT(*) AS pct FROM people WHERE id<0",
                "task_spec": spec,
            },
            {
                "final_sql": "SELECT 100.0*SUM(score=10)/COUNT(*) AS pct FROM people",
                "task_spec": spec,
            },
        ],
        question=question,
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["undefined_percentage_check_population"]
    assert "WHERE id<0" not in result["final_sql"]


def test_native_unavailable_method_has_a_typed_exit_without_fabricating_results(database):
    from langchain_core.messages import AIMessage

    class Native:
        def bind(self, **kwargs):
            assert "declare_unsupported" in {tool["function"]["name"] for tool in kwargs["tools"]}
            assert kwargs["tool_choice"] == "required"
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "declare_unsupported",
                        "args": {"reason": "p-value unavailable; do-not-log"},
                        "id": "one",
                    }
                ],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    task = Task("infiagent-dabench", "synthetic", "p-value?", "@p[value]", "hard", "people")
    result = BoundedSQLAgent(Native(), tool_profile="sql-semantic").run(task, database)
    assert result["failure"] == "unsupported_capability" and not result["answers"]
    assert result["trace"] == [
        {
            "step": 1,
            "tool": "capability_declaration",
            "status": "unsupported",
            "code": "unsupported_capability",
            "response_mode": "native_function",
        }
    ]
    assert "do-not-log" not in json.dumps(result)


def test_public_percent_placeholder_flows_into_automatic_answer_binding(database):
    result = run(
        database,
        [
            {"sql": "SELECT 100.0*SUM(score=10)/COUNT(*) AS rate FROM people"},
            {"submit_answers": {"fields": [{"name": "rate", "step": 1, "path": ["rows", 0, 0]}]}},
        ],
        benchmark="infiagent-dabench",
        question="matching rate?",
        instructions=(
            '@rate[rate_%] where "rate_%" is a positive real value, rounded to two decimal places.'
        ),
    )
    assert result["failure"] is None and result["answers"] == {"rate": "100.00"}
    assert result["task_contract"]["answer"]["fields"]["rate"]["kind"] == "number"
