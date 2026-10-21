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


def run(
    database,
    actions,
    benchmark="bird-mini-dev-sqlite",
    question="champions",
    instructions="",
    profile="sql-semantic",
):
    iterator = iter(actions)

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(iterator)),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    task = Task(benchmark, "synthetic", question, instructions, "easy", "data")
    return BoundedSQLAgent(Model(), tool_profile=profile, max_steps=len(actions)).run(
        task, database
    )


def contract(question="champions", instructions="", **answer):
    return {
        "contract": {"question": question, "public_requirements": instructions, "answer": answer}
    }


def test_observed_token_countdown_preserves_spend_after_rejected_action(database):
    countdown = []
    actions = iter(
        [
            {"sql": 123},
            {"submit_sql": {"sql": "SELECT id FROM people", "output_columns": ["id"]}},
        ]
    )

    class Model:
        def invoke(self, messages):
            budget = json.loads(messages[-1][1])
            countdown.append((budget["remaining_actions"], budget["remaining_observed_tokens"]))
            assert "does not submit" in budget["submission_hint"]
            return SimpleNamespace(
                content=json.dumps(next(actions)),
                usage_metadata={"total_tokens": 17},
                response_metadata={},
            )

    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2, max_tokens=100).run(
        Task("bird-mini-dev-sqlite", "synthetic", "List ids", "", "easy", "data"), database
    )
    assert countdown == [(2, 100), (1, 83)]
    assert result["tokens"] == 34 and result["failure"] is None


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


def test_schema_rejected_multi_action_is_summarized_without_fabricated_tool_results():
    from langchain_core.messages import AIMessage, ToolMessage

    from generaldataagent.evaluation.stage_seven import ToolConversation

    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_sql", "args": {"sql": "SELECT " + "x" * 8000}, "id": "one"},
            {"name": "submit_sql", "args": {"sql": "SELECT y"}, "id": "two"},
        ],
    )
    transcript = ToolConversation([("system", "read only")])
    transcript.record_calls(response)
    transcript.append(
        (
            "human",
            json.dumps(
                {
                    "code": "action_schema",
                    "schema_errors": [{"path": ["tool_calls"], "type": "one_action"}],
                }
            ),
        )
    )
    assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in transcript)
    summary = json.loads(transcript[-1][1])
    assert summary["rejected_unexecuted_calls"] == 2
    assert summary["rejected_arguments_omitted"] is True
    assert "x" * 100 not in transcript[-1][1] and not transcript.pending
    assert response.tool_calls[0]["args"]["sql"].endswith("x" * 8000)


def test_multi_action_summary_keeps_spent_budget_and_executes_neither_request(
    database, monkeypatch
):
    from langchain_core.messages import AIMessage, ToolMessage

    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    executed = []
    original = SQLiteReadOnlyExecutor.execute

    def observe(self, sql, parameters=()):
        executed.append(sql)
        return original(self, sql, parameters)

    monkeypatch.setattr(SQLiteReadOnlyExecutor, "execute", observe)

    class Native:
        calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                calls = [
                    {
                        "name": "execute_sql",
                        "args": {"sql": "SELECT id FROM people WHERE id=987654"},
                        "id": "one",
                    },
                    {
                        "name": "submit_sql",
                        "args": {"sql": "SELECT 987654 FROM people", "output_columns": ["wrong"]},
                        "id": "two",
                    },
                ]
            else:
                assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in messages)
                assert any(
                    isinstance(message, tuple) and '"rejected_unexecuted_calls": 2' in message[1]
                    for message in messages
                )
                calls = [
                    {
                        "name": "submit_sql",
                        "args": {
                            "sql": "SELECT COUNT(*) AS n FROM people",
                            "output_columns": ["n"],
                            "shape": "scalar",
                        },
                        "id": "finish",
                    }
                ]
            return AIMessage(
                content="",
                tool_calls=calls,
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    before = database.read_bytes()
    model = Native()
    result = BoundedSQLAgent(model, tool_profile="sql-semantic", max_steps=2).run(
        Task("bird-mini-dev-sqlite", "synthetic", "How many people?", "", "easy", "people"),
        database,
    )
    assert result["failure"] is None and result["tokens"] == 20 and model.calls == 2
    assert result["trace"][0]["code"] == "action_schema"
    assert len([e for e in result["trace"] if e.get("status") == "ok"]) == 1
    assert not any("987654" in sql for sql in executed)
    assert database.read_bytes() == before


def test_public_percentage_scale_rejects_unscaled_fraction_before_final_submission(database):
    result = run(
        database,
        [
            {
                "final_sql": "SELECT 1.0*(MAX(score)-MIN(score))/NULLIF(MIN(score),0) "
                "AS rate FROM people",
                "task_spec": {"answer": {"columns": ["rate"], "shape": "scalar"}},
            },
            {
                "final_sql": "SELECT 100.0*(MAX(score)-MIN(score))/NULLIF(MIN(score),0) "
                "AS rate FROM people",
                "task_spec": {"answer": {"columns": ["rate"], "shape": "scalar"}},
            },
        ],
        question="What is the growth rate from start to end?",
        instructions="growth = [(ending-starting)/starting] * 100%",
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["change_percent_scale"]
    assert len([e for e in result["trace"] if e.get("status") == "ok"]) == 1


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
            expected = {
                "compute_sql",
                "fixed_analysis",
                "search_documents",
                "collect_values",
                "submit_answers",
                "declare_unsupported",
                "submit_sql_answers",
            }
            if not self.calls:
                expected.remove("submit_answers")
            assert {t["function"]["name"] for t in kwargs["tools"]} == expected
            if self.calls:
                tool = next(t for t in kwargs["tools"] if t["function"]["name"] == "submit_answers")
                properties = tool["function"]["parameters"]["properties"]["fields"]["items"][
                    "properties"
                ]
                assert properties["name"]["enum"] == ["mean"]
                assert properties["step"]["enum"] == [1]
                assert "rule" in properties  # No explicit public per-field type here.
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


def test_sql_answers_compute_bind_and_round_once_with_full_receipts(database):
    instructions = (
        '@mean[average] @n[count] where "average" is a number rounded to two decimal places. '
        '"count" is an integer.'
    )
    result = run(
        database,
        [{"submit_sql_answers": {"sql": "SELECT COUNT(*) AS n, AVG(id) AS mean FROM people"}}],
        benchmark="infiagent-dabench",
        question="mean and count?",
        instructions=instructions,
    )
    assert result["failure"] is None and len(result["trace"]) == 1
    assert result["answers"] == {"mean": "1.50", "n": 2}
    assert result["answer_evidence"]["mean"]["raw_value"] == 1.5
    assert result["answer_evidence"]["mean"]["result_hash"] == result["trace"][0]["result_hash"]
    assert result["trace"][0]["auto_bound_public_keys"] == ["mean", "n"]


def test_inline_mixed_precision_sql_answers_are_assembled_without_contract_retry(database):
    result = run(
        database,
        [
            {
                "submit_sql_answers": {
                    "sql": (
                        "SELECT COUNT(*) AS n, AVG(id) AS mean, AVG(score)/3.0 AS first FROM people"
                    )
                }
            }
        ],
        benchmark="infiagent-dabench",
        question="Summarize values",
        instructions=(
            "@n[Number of entries after cleaning], "
            "@mean[Mean value, rounded to two decimal places], "
            "@first[First transformed value, rounded to three decimal places]"
        ),
    )
    assert result["failure"] is None
    assert result["answers"] == {"n": 2, "mean": "1.50", "first": "3.333"}
    assert len(result["trace"]) == 1
    assert result["answer_evidence"]["first"]["raw_value"] == 10 / 3


def test_ambiguous_mixed_precision_atomic_contract_error_is_actionable(database):
    result = run(
        database,
        [{"submit_sql_answers": {"sql": "SELECT AVG(id) AS x FROM people"}}],
        benchmark="infiagent-dabench",
        question="Summarize values",
        instructions="@x[Value rounded to two decimal places or three decimal places]",
    )
    assert result["trace"][0]["code"] == "semantic_contract"
    assert result["trace"][0]["repair_type"] == "revise_plan_or_query"


@pytest.mark.parametrize(
    "sql,check",
    [
        ("SELECT AVG(id) AS wrong FROM people", "answer_sql_requires_public_column_aliases"),
        ("SELECT id AS mean FROM people", "answer_sql_requires_one_row"),
        ("SELECT 999 AS mean", "data_dependent_answer_required"),
    ],
)
def test_atomic_sql_rejects_missing_keys_multiple_rows_and_fabricated_values(database, sql, check):
    result = run(
        database,
        [
            {"submit_sql_answers": {"sql": sql}},
            {"submit_sql_answers": {"sql": "SELECT AVG(id) AS mean FROM people"}},
        ],
        benchmark="infiagent-dabench",
        question="mean?",
        instructions="@mean[value]",
    )
    assert result["failure"] is None and result["answers"] == {"mean": 1.5}
    assert result["trace"][0]["checks"] == [check]
    assert result["answer_evidence"]["mean"]["step"] == 2


def test_native_atomic_query_finishes_without_a_second_binding_call(database):
    from langchain_core.messages import AIMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            assert "submit_sql_answers" in {tool["function"]["name"] for tool in kwargs["tools"]}
            return self

        def invoke(self, messages):
            self.calls += 1
            assert self.calls == 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_sql_answers",
                        "args": {"sql": "SELECT COUNT(*) AS n FROM people"},
                        "id": "atomic",
                    }
                ],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    model = Native()
    task = Task("infiagent-dabench", "synthetic", "count?", "@n[value]", "easy", "people")
    result = BoundedSQLAgent(model, tool_profile="sql-semantic", max_steps=1).run(task, database)
    assert result["failure"] is None and result["answers"] == {"n": 2}
    assert result["trace"][0]["response_mode"] == "native_function"
    assert result["answer_evidence"]["n"]["step"] == 1


def test_native_query_plan_compiles_to_final_sql_in_one_opt_in_action(database):
    from langchain_core.messages import AIMessage

    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    class Native:
        def bind(self, **kwargs):
            assert {tool["function"]["name"] for tool in kwargs["tools"]} == {
                "submit_sql",
                "submit_query_plan",
                "plan_analysis",
                "execute_sql",
                "declare_unsupported",
            }
            assert kwargs["tool_choice"] == "required"
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_query_plan",
                        "id": "plan",
                        "args": {
                            "operation": "rank",
                            "relations": ["people"],
                            "entity_keys": [{"relation": "people", "column": "id"}],
                            "projections": [
                                {"field": {"relation": "people", "column": "id"}, "name": "id"}
                            ],
                            "metric": {"relation": "people", "column": "score"},
                            "source_excerpt": "top 1 by score",
                        },
                    }
                ],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    before = database.read_bytes()
    task = Task("bird-mini-dev-sqlite", "synthetic", "top 1 by score", "", "easy", "people")
    result = BoundedSQLAgent(Native(), tool_profile="sql-semantic-plan", max_steps=1).run(
        task, database
    )
    assert result["failure"] is None and len(result["trace"]) == 1
    assert SQLiteReadOnlyExecutor(database).execute(result["final_sql"]).rows == ((1,), (2,))
    assert result["trace"][0]["compiled_query_plan"]["ties"] == "all"
    assert database.read_bytes() == before


def test_query_plan_profile_is_explicit_and_sql_output_only(database):
    action = {
        "submit_query_plan": {
            "operation": "count",
            "relations": ["people"],
            "entity_keys": [{"relation": "people", "column": "id"}],
            "source_excerpt": "champions",
        }
    }
    result = run(
        database,
        [action, {"submit_sql": {"sql": "SELECT id FROM people", "output_columns": ["id"]}}],
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["query_plan_requires_unplanned_opt_in_task"]
    with pytest.raises(ValueError, match="SQL-output"):
        run(
            database,
            [{"unsupported": "method"}],
            benchmark="infiagent-dabench",
            profile="sql-semantic-plan",
        )


def test_undefined_population_feedback_reuses_public_scoped_literal_evidence(database):
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE items(name TEXT, group_id INT, power INT);"
            "CREATE TABLE groups(id INT,name TEXT); INSERT INTO groups VALUES(1,'Winter');"
            "INSERT INTO items VALUES('A',1,1),('B',1,0);"
        )

    observed_feedback = []

    class Model:
        calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                sql = "SELECT SUM(power)*100.0/COUNT(*) AS pct FROM items WHERE name='Winter'"
            else:
                feedback = next(
                    payload
                    for role, content in reversed(messages)
                    if role == "human" and "error" in (payload := json.loads(content))
                )
                observed_feedback.append(feedback)
                sql = (
                    "SELECT SUM(i.power)*100.0/COUNT(*) AS pct FROM items i "
                    "JOIN groups g ON i.group_id=g.id WHERE g.name='Winter'"
                )
            return SimpleNamespace(
                content=json.dumps(
                    {"submit_sql": {"sql": sql, "output_columns": ["pct"], "shape": "scalar"}}
                ),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    task = Task(
        "bird-mini-dev-sqlite",
        "synthetic",
        "percentage of eligible items in group Winter?",
        "group Winter refers to name = 'Winter'",
        "easy",
        "items",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(task, database)
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["undefined_percentage_check_population"]
    feedback = observed_feedback[0]
    observations = feedback["public_population_observations"][0]["literal_observations"]
    assert {row["candidate"][0]: row["exact_match_rows"] for row in observations} == {
        "groups": 1,
        "items": 0,
    }
    assert "Do not merely add NULLIF" in feedback["hint"]


def test_opt_in_native_staged_contract_crosscheck_and_final_submission(database):
    from langchain_core.messages import AIMessage, ToolMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert kwargs["tool_choice"] == "required"
            assert kwargs["parallel_tool_calls"] is False
            if self.calls == 0:
                assert "plan_analysis" in names and "crosscheck_sql" not in names
            else:
                assert "crosscheck_sql" in names and "plan_analysis" not in names
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                name, args = "plan_analysis", {"answer": {"columns": ["id"], "shape": "rows"}}
            elif self.calls == 2:
                assert any(
                    isinstance(item, ToolMessage) and "accepted" in item.content
                    for item in messages
                )
                name, args = (
                    "crosscheck_sql",
                    {"sql": "SELECT DISTINCT id FROM people", "purpose": "entity_grain"},
                )
            else:
                name, args = "submit_sql", {"sql": "SELECT id FROM people"}
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "id": str(self.calls), "args": args}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    before = database.read_bytes()
    task = Task("bird-mini-dev-sqlite", "synthetic", "list all people ids", "", "easy", "people")
    result = BoundedSQLAgent(Native(), tool_profile="sql-semantic-plan", max_steps=3).run(
        task, database
    )
    assert result["failure"] is None
    assert result["trace"][0]["status"] == "planned"
    assert len(result["trace"]) == 3
    assert result["final_sql"] == "SELECT id FROM people"
    assert result["task_contract"]["question"] == task.question
    assert database.read_bytes() == before


def test_percentage_final_cannot_replace_ratio_with_intermediate_counts(database):
    actions = [
        {
            "submit_sql": {
                "sql": "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN score=10 THEN 1 ELSE 0 END) AS matches FROM people",
                "output_columns": ["n", "matches"],
            }
        },
        {
            "submit_sql": {
                "sql": "SELECT 100.0*SUM(CASE WHEN score=10 THEN 1 ELSE 0 END)"
                "/COUNT(*) AS pct FROM people",
                "output_columns": ["pct"],
                "shape": "scalar",
            }
        },
    ]
    result = run(database, actions, question="What is the percentage of people with score 10?")
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["percentage_requires_ratio_not_counts"]
    assert result["task_contract"]["answer"]["columns"] == ["pct"]
    assert len(result["trace"]) == 2


def test_all_object_denominator_null_filter_repair_preserves_requested_population(database):
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO people VALUES(3,'1980-01-01',NULL)")
    actions = [
        {
            "submit_sql": {
                "sql": "SELECT SUM(score)*1.0/COUNT(*) AS average FROM people "
                "WHERE score IS NOT NULL",
                "output_columns": ["average"],
                "shape": "scalar",
            }
        },
        {
            "submit_sql": {
                "sql": "SELECT SUM(score)*1.0/COUNT(*) AS average FROM people",
                "output_columns": ["average"],
                "shape": "scalar",
            }
        },
    ]
    result = run(
        database,
        actions,
        question="Calculate average score for all people.",
        instructions="average = DIVIDE(SUM(score), COUNT(all people));",
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["unrequested_all_count_null_filter"]
    assert SQLiteReadOnlyExecutor(database).execute(result["final_sql"]).rows[0][
        0
    ] == pytest.approx(20 / 3)


@pytest.mark.parametrize(
    "sql,columns",
    [
        ("SELECT 2 AS n", ["n"]),
        ("WITH guessed AS (SELECT 2 AS n) SELECT n FROM guessed", ["n"]),
        ("SELECT COUNT(*) AS n, 0.99 AS score FROM people", ["n", "score"]),
    ],
)
def test_sql_output_cannot_certify_constants_as_database_answers(database, sql, columns):
    actions = [
        {"submit_sql": {"sql": sql, "output_columns": columns}},
        {
            "submit_sql": {
                "sql": "SELECT COUNT(*) AS n FROM people",
                "output_columns": ["n"],
                "shape": "scalar",
            }
        },
    ]
    result = run(database, actions, question="How many people?")
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["data_dependent_sql_required"]
    assert result["final_sql"] == "SELECT COUNT(*) AS n FROM people"


def test_staged_native_style_contract_accepts_unambiguous_root_source_without_mutation(database):
    from generaldataagent.contracts.semantic_requirements import SemanticRequirements

    question = "Return people tied for the highest score."
    requirements = {
        "predicates": [
            {
                "expression": "score=(SELECT MAX(score) FROM people)",
                "scope": "people",
                "source_excerpt": question,
            }
        ]
    }
    actions = [
        {
            "plan_analysis": {
                "answer": {"columns": ["id"], "shape": "rows", "ties": "all"},
                "requirements": requirements,
            }
        },
        {
            "crosscheck_sql": {
                "sql": "SELECT id FROM (SELECT id,RANK() OVER(ORDER BY score DESC) AS r "
                "FROM people) WHERE r=1",
                "purpose": "ties",
            }
        },
        {
            "submit_sql": {
                "sql": "SELECT id FROM people WHERE score=(SELECT MAX(score) FROM people)"
            }
        },
    ]
    result = run(database, actions, question=question, profile="sql-semantic-plan")
    assert result["failure"] is None
    assert (
        result["task_contract"]["requirements"]
        == SemanticRequirements.model_validate(requirements).model_dump()
    )
    assert len(result["trace"]) == 3
    assert result["trace"][1]["tool"] == "sql_crosscheck"


def test_public_collection_auto_contract_requires_complete_values_and_bindings(database):
    instructions = "@ids[id1, id2,...] The answer should not contain any duplicates."
    actions = [
        {"submit_sql_answers": {"sql": "SELECT group_concat(id) AS ids FROM people"}},
        {"collect_values": {"sql": "SELECT DISTINCT id FROM people ORDER BY id"}},
        {"submit_answers": {"fields": [{"name": "ids", "step": 2, "path": ["items"]}]}},
    ]
    result = run(
        database,
        actions,
        benchmark="infiagent-dabench",
        question="List all people ids.",
        instructions=instructions,
    )
    assert result["failure"] is None and result["answers"] == {"ids": "1, 2"}
    assert result["task_contract"]["answer"]["fields"]["ids"]["kind"] == "set"
    assert result["answer_evidence"]["ids"]["method_id"] == "complete_collection"
    assert result["answer_evidence"]["ids"]["step"] == 2
    assert result["trace"][0]["status"] == "error"


def test_public_boolean_type_cannot_be_changed_by_optional_binding_rule(database):
    instructions = "@flag[value] where 'value' is a boolean."
    actions = [
        {"compute_sql": {"sql": "SELECT COUNT(*)>0 AS flag FROM people"}},
        {
            "submit_answers": {
                "fields": [
                    {"name": "flag", "step": 1, "path": ["rows", 0, 0], "rule": {"kind": "integer"}}
                ]
            }
        },
        {"submit_answers": {"fields": [{"name": "flag", "step": 1, "path": ["rows", 0, 0]}]}},
    ]
    result = run(
        database,
        actions,
        benchmark="infiagent-dabench",
        question="Are there any people?",
        instructions=instructions,
    )
    assert result["failure"] is None and result["answers"]["flag"] is True
    assert result["trace"][1]["status"] == "error"
    assert result["answer_evidence"]["flag"]["submitted_type"] == "bool"


@pytest.mark.parametrize("profile", ["sql-precision", "sql-semantic"])
def test_final_sql_can_verify_complete_wide_results_above_exploration_limit(database, profile):
    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO people VALUES(?,'1980-01-01',1)", [(i,) for i in range(3, 303)])
    before = database.read_bytes()
    sql = "SELECT id,score FROM people ORDER BY id"
    action = (
        {"final_sql": sql}
        if profile == "sql-precision"
        else {"submit_sql": {"sql": sql, "output_columns": ["id", "score"]}}
    )
    result = run(database, [action], profile=profile, question="List all people ids and scores.")
    assert result["failure"] is None and result["final_sql"] == sql
    assert result["trace"][0]["row_count"] == 302
    assert database.read_bytes() == before


@pytest.mark.parametrize("profile", ["sql-precision", "sql-semantic"])
def test_final_sql_cell_budget_is_shared_by_both_arms_and_never_silently_truncates(
    database, profile
):
    from generaldataagent.evaluation.stage_six_protocol import evaluation_protocol

    cells = evaluation_protocol(workers=1)["final_and_crosscheck_cell_limit"]
    with sqlite3.connect(database) as db:
        db.executemany(
            "INSERT INTO people VALUES(?,'1980-01-01',1)", [(i,) for i in range(3, cells // 2 + 2)]
        )
    sql = "SELECT id,score FROM people"
    action = (
        {"final_sql": sql}
        if profile == "sql-precision"
        else {"submit_sql": {"sql": sql, "output_columns": ["id", "score"]}}
    )
    result = run(database, [action], profile=profile, question="List all people ids and scores.")
    assert result["failure"] == "execution_or_model_error"
    assert result["final_sql"] is None and result["trace"][0]["code"] == "result_limit"


def test_crosscheck_large_collection_stays_complete_with_one_check_budget(database):
    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO people VALUES(?,'1980-01-01',1)", [(i,) for i in range(3, 303)])
    actions = [
        {"plan_analysis": {"answer": {"columns": ["id"], "shape": "set"}}},
        {"crosscheck_sql": {"sql": "SELECT id FROM people ORDER BY id", "purpose": "entity_grain"}},
        {"submit_sql": {"sql": "SELECT DISTINCT id FROM people ORDER BY id"}},
    ]
    result = run(database, actions, profile="sql-semantic-plan", question="List all people ids.")
    assert result["failure"] is None
    assert result["trace"][1]["tool"] == "sql_crosscheck"
    assert result["trace"][2]["row_count"] == 302
    exploratory = run(database, [{"sql": "SELECT id FROM people"}], profile="sql-precision")
    assert exploratory["trace"][0]["code"] == "result_limit"


def test_collection_presentation_keeps_full_raw_value_and_bound_receipt(database):
    instructions = "@ids[id1, id2,…] No duplicates."
    actions = [
        {"collect_values": {"sql": "SELECT DISTINCT id FROM people ORDER BY id"}},
        {"submit_answers": {"fields": [{"name": "ids", "step": 1, "path": ["items"]}]}},
    ]
    result = run(
        database,
        actions,
        benchmark="infiagent-dabench",
        question="List ids.",
        instructions=instructions,
    )
    assert result["failure"] is None and result["answers"] == {"ids": "1, 2"}
    receipt = result["answer_evidence"]["ids"]
    assert receipt["raw_value"] == [1, 2] and receipt["raw_type"] == "list"
    assert receipt["submitted_value"] == "1, 2" and receipt["submitted_type"] == "str"
    assert receipt["method_id"] == "complete_collection"


def test_opt_in_json_sql_infers_only_observed_projections_and_preserves_public_texts(database):
    class JsonOnly:
        def bind(self, **kwargs):
            raise AssertionError("JSON profile must not bind native tools")

        def invoke(self, messages):
            request = json.loads(messages[1][1])
            assert "relationships" in request
            assert "relationships" not in request["semantic_context"]
            return SimpleNamespace(
                content=json.dumps({"final_sql": "SELECT COUNT(*) AS n FROM people"}),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    before = database.read_bytes()
    task = Task(
        "bird-mini-dev-sqlite",
        "synthetic",
        "How many people?",
        "Use the whole table.",
        "easy",
        "people",
    )
    result = BoundedSQLAgent(JsonOnly(), tool_profile="sql-semantic-json", max_steps=1).run(
        task, database
    )
    assert result["failure"] is None and result["trace"][0]["response_mode"] == "json"
    assert result["task_contract"]["question"] == task.question
    assert result["task_contract"]["public_requirements"] == task.instructions
    assert result["task_contract"]["answer"]["columns"] == ["n"]
    assert database.read_bytes() == before


def test_json_sql_mode_keeps_column_and_data_dependency_guards(database):
    result = run(
        database,
        [
            {"final_sql": "SELECT absent FROM people"},
            {"final_sql": "SELECT 2 AS n"},
            {"final_sql": "SELECT COUNT(*) AS n FROM people"},
        ],
        profile="sql-semantic-json",
    )
    assert result["failure"] is None
    assert result["trace"][0]["code"] == "unknown_column"
    assert result["trace"][1]["checks"] == ["data_dependent_sql_required"]
    assert result["task_contract"]["answer"]["columns"] == ["n"]
    with pytest.raises(ValueError, match="SQL-output"):
        run(
            database,
            [{"unsupported": "x"}],
            benchmark="infiagent-dabench",
            profile="sql-semantic-json",
        )


def test_sql_output_json_compatibility_cannot_submit_bound_answers(database):
    result = run(
        database,
        [
            contract(columns=["n"]),
            {"sql": "SELECT COUNT(*) AS n FROM people"},
            {"answers": {}, "evidence_steps": [2], "bindings": {"n": [2, ["rows", 0, 0]]}},
        ],
    )
    assert result["failure"] and result["final_sql"] is None and not result["answers"]
    assert result["trace"][-1]["checks"] == ["sql_output_requires_sql_action"]


def test_public_age_direction_repair_uses_same_bounded_sql_loop(database):
    before = database.read_bytes()
    result = run(
        database,
        [
            {"final_sql": "SELECT id FROM people ORDER BY birthday DESC LIMIT 1"},
            {
                "final_sql": "SELECT id FROM people "
                "WHERE birthday=(SELECT MIN(birthday) FROM people)"
            },
        ],
        question="List IDs of the oldest people.",
        instructions="The larger the birthday value, the younger the person is.",
        profile="sql-semantic-json",
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["public_age_order_direction"]
    assert result["trace"][1]["status"] == "ok"
    assert "MIN(birthday)" in result["final_sql"]
    assert database.read_bytes() == before


def test_public_age_direction_hint_describes_public_semantics_without_input_values():
    from generaldataagent.evaluation.stage_six_repair import execution_hint

    hint = execution_hint(ValueError("public_age_order_direction"))
    assert "public instructions" in hint and "oldest/youngest" in hint


def test_list_collection_preserves_duplicate_values_and_full_receipt(database):
    before = database.read_bytes()
    result = run(
        database,
        [
            {
                "collect_values": {
                    "sql": "SELECT score FROM people ORDER BY score",
                    "collection_kind": "list",
                }
            },
            {"submit_answers": {"fields": [{"name": "scores", "step": 1, "path": ["items"]}]}},
        ],
        benchmark="infiagent-dabench",
        question="List every score, including repeated values.",
        instructions='@scores[values] where "values" is a list of integers in ascending order.',
    )
    assert result["failure"] is None and result["answers"] == {"scores": [10, 10]}
    assert result["answer_evidence"]["scores"]["raw_value"] == [10, 10]
    assert result["answer_evidence"]["scores"]["method_id"] == "complete_collection"
    assert database.read_bytes() == before


def test_public_list_order_is_checked_without_silently_sorting_evidence(database):
    result = run(
        database,
        [
            {
                "collect_values": {
                    "sql": "SELECT id FROM people ORDER BY id DESC",
                    "collection_kind": "list",
                }
            },
            {"submit_answers": {"fields": [{"name": "ids", "step": 1, "path": ["items"]}]}},
            {
                "collect_values": {
                    "sql": "SELECT id FROM people ORDER BY id ASC",
                    "collection_kind": "list",
                }
            },
            {"submit_answers": {"fields": [{"name": "ids", "step": 3, "path": ["items"]}]}},
        ],
        benchmark="infiagent-dabench",
        instructions='@ids[values] where "values" is a list of integers in ascending order.',
    )
    assert result["failure"] is None and result["answers"] == {"ids": [1, 2]}
    assert result["trace"][1]["status"] == "error"
    assert result["answer_evidence"]["ids"]["step"] == 3


def test_list_mode_cannot_bypass_public_set_uniqueness(database):
    result = run(
        database,
        [
            {"collect_values": {"sql": "SELECT score FROM people", "collection_kind": "list"}},
            {"submit_answers": {"fields": [{"name": "scores", "step": 1, "path": ["items"]}]}},
        ],
        benchmark="infiagent-dabench",
        instructions='@scores[values] where "values" is a set of integers. No duplicates.',
    )
    assert result["failure"] and not result["answers"]
    assert "duplicate_entities" in result["trace"][-1]["checks"]


def test_data_dependent_sql_answers_still_obey_public_closed_string_domain(database):
    result = run(
        database,
        [
            {
                "submit_sql_answers": {
                    "sql": "SELECT CASE WHEN COUNT(*)>0 THEN 'PASS' ELSE 'FAIL' END AS status "
                    "FROM people"
                }
            },
            {
                "submit_sql_answers": {
                    "sql": "SELECT CASE WHEN COUNT(*)>0 THEN 'pass' ELSE 'fail' END AS status "
                    "FROM people"
                }
            },
        ],
        benchmark="infiagent-dabench",
        instructions='@status[label] where "label" is a string '
        'that can either be "pass" or "fail".',
    )
    assert result["failure"] is None and result["answers"] == {"status": "pass"}
    assert result["trace"][0]["status"] == "error"
    assert result["answer_evidence"]["status"]["step"] == 2


def test_mixed_collection_scalar_task_reuses_collection_after_atomic_shortcut_rejection(database):
    result = run(
        database,
        [
            {
                "collect_values": {
                    "sql": "SELECT id FROM people ORDER BY id",
                    "collection_kind": "list",
                }
            },
            {"submit_sql_answers": {"sql": "SELECT COUNT(*) AS ids,COUNT(*) AS n FROM people"}},
            {"compute_sql": {"sql": "SELECT COUNT(*) AS n FROM people"}},
            {
                "submit_answers": {
                    "fields": [
                        {"name": "ids", "step": 1, "path": ["items"]},
                        {"name": "n", "step": 3, "path": ["rows", 0, 0]},
                    ]
                }
            },
        ],
        benchmark="infiagent-dabench",
        instructions='@ids[values], @n[count_value]\nwhere "values" is a list of integers '
        'in ascending order.\nwhere "count_value" is an integer.',
    )
    assert result["failure"] is None and result["answers"] == {"ids": [1, 2], "n": 2}
    assert result["trace"][1]["checks"] == ["answer_sql_requires_scalar_public_fields"]
    assert result["answer_evidence"]["ids"]["step"] == 1


@pytest.mark.parametrize("large_collection", [False, True])
def test_mixed_atomic_sql_binds_complete_collection_and_new_scalars_in_two_actions(
    database, large_collection
):
    from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors

    if large_collection:
        with sqlite3.connect(database) as db:
            db.executemany(
                "INSERT INTO people VALUES(?,'1980-01-01',10)", [(i,) for i in range(3, 303)]
            )
    expected_n = 302 if large_collection else 2
    before = database.read_bytes()
    result = run(
        database,
        [
            {
                "collect_values": {
                    "sql": "SELECT id FROM people ORDER BY id",
                    "collection_kind": "list",
                }
            },
            {
                "submit_mixed_sql_answers": {
                    "sql": "SELECT COUNT(*) AS n FROM people",
                    "collections": [{"name": "ids", "step": 1}],
                }
            },
        ],
        benchmark="infiagent-dabench",
        instructions=(
            "@ids[values], @n[count_value]\n"
            'where "values" is a list of integers in ascending order.\n'
            'where "count_value" is an integer.'
        ),
    )
    assert result["failure"] is None and result["answers"] == {
        "ids": list(range(1, expected_n + 1)),
        "n": expected_n,
    }
    assert result["answer_evidence"]["ids"]["step"] == 1
    assert result["answer_evidence"]["n"]["step"] == 2
    assert len(result["trace"]) == 2 and result["trace"][1]["auto_bound_public_keys"] == [
        "ids",
        "n",
    ]
    report = {
        "benchmark": "infiagent-dabench",
        "results": [result | {"case_id": "synthetic", "passed": True}],
    }
    assert answer_receipt_errors(report) == []
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "sql,bindings,first_sql",
    [
        (
            "SELECT 0 AS n FROM people LIMIT 1",
            [{"name": "ids", "step": 1}],
            "SELECT id FROM people",
        ),
        ("SELECT id AS n FROM people", [{"name": "ids", "step": 1}], "SELECT id FROM people"),
        (
            "SELECT id AS n FROM people WHERE id<0",
            [{"name": "ids", "step": 1}],
            "SELECT id FROM people",
        ),
        (
            "SELECT COUNT(*) AS wrong FROM people",
            [{"name": "ids", "step": 1}],
            "SELECT id FROM people",
        ),
        (
            "SELECT COUNT(*) AS n FROM people",
            [{"name": "unknown", "step": 1}],
            "SELECT id FROM people",
        ),
        ("SELECT COUNT(*) AS n FROM people", [{"name": "ids", "step": 2}], "SELECT id FROM people"),
        (
            "SELECT COUNT(*) AS n FROM people",
            [{"name": "ids", "step": 99}],
            "SELECT id FROM people",
        ),
        (
            "SELECT COUNT(*) AS n FROM people",
            [{"name": "ids", "step": 1}, {"name": "ids", "step": 1}],
            "SELECT id FROM people",
        ),
        ("DELETE FROM people", [{"name": "ids", "step": 1}], "SELECT id FROM people"),
        (
            "SELECT COUNT(*) AS n FROM people",
            [{"name": "ids", "step": 1, "path": ["items", 0]}],
            "SELECT id FROM people",
        ),
        (
            "SELECT COUNT(*) AS n FROM people",
            [{"name": "ids", "step": 1}],
            "SELECT id FROM people ORDER BY id DESC",
        ),
    ],
)
def test_mixed_atomic_sql_rejects_forged_or_incomplete_inputs(database, sql, bindings, first_sql):
    before = database.read_bytes()
    result = run(
        database,
        [
            {"collect_values": {"sql": first_sql, "collection_kind": "list"}},
            {"submit_mixed_sql_answers": {"sql": sql, "collections": bindings}},
        ],
        benchmark="infiagent-dabench",
        instructions=(
            "@ids[values], @n[count_value]\n"
            'where "values" is a list of integers in ascending order.\n'
            'where "count_value" is an integer.'
        ),
    )
    assert result["failure"] and not result["answers"] and not result["answer_evidence"]
    assert result["trace"][-1]["status"] == "error"
    assert database.read_bytes() == before


def test_mixed_atomic_sql_cannot_treat_an_exploratory_row_container_as_a_complete_list(database):
    result = run(
        database,
        [
            {"compute_sql": {"sql": "SELECT id FROM people"}},
            {
                "submit_mixed_sql_answers": {
                    "sql": "SELECT COUNT(*) AS n FROM people",
                    "collections": [{"name": "ids", "step": 1}],
                }
            },
        ],
        benchmark="infiagent-dabench",
        instructions=(
            '@ids[values], @n[count_value]\nwhere "values" is a list of integers.\n'
            'where "count_value" is an integer.'
        ),
    )
    assert result["failure"] and not result["answers"]
    assert result["trace"][-1]["checks"] == ["mixed_sql_requires_prior_complete_collection"]


@pytest.mark.parametrize("empty_collection", [False, True])
def test_native_mixed_atomic_tool_only_uses_available_collection_steps(database, empty_collection):
    from langchain_core.messages import AIMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            tools = {item["function"]["name"]: item["function"] for item in kwargs["tools"]}
            assert "submit_sql_answers" not in tools
            if not self.calls:
                assert "submit_mixed_sql_answers" not in tools
            else:
                schema = tools["submit_mixed_sql_answers"]["parameters"]
                item = schema["properties"]["collections"]["items"]
                assert item["properties"]["name"]["enum"] == ["ids"]
                assert item["properties"]["step"]["enum"] == [1]
                assert "path" not in item["properties"]
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                name, args = (
                    "collect_values",
                    {
                        "sql": "SELECT id FROM people"
                        + (" WHERE id<0" if empty_collection else "")
                        + " ORDER BY id",
                        "collection_kind": "list",
                    },
                )
            else:
                name, args = (
                    "submit_mixed_sql_answers",
                    {
                        "sql": "SELECT COUNT(*) AS n FROM people",
                        "collections": [{"name": "ids", "step": 1}],
                    },
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(self.calls)}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    instructions = (
        "@ids[values], @n[count_value]\n"
        'where "values" is a list of integers in ascending order.\n'
        'where "count_value" is an integer.'
    )
    question = (
        "List IDs less than zero and count all people."
        if empty_collection
        else "List IDs and count all people."
    )
    task = Task("infiagent-dabench", "synthetic", question, instructions, "easy", "people")
    before = database.read_bytes()
    model = Native()
    result = BoundedSQLAgent(model, tool_profile="sql-semantic", max_steps=2).run(task, database)
    assert result["failure"] is None and model.calls == 2
    assert result["answers"] == {"ids": [] if empty_collection else [1, 2], "n": 2}
    assert result["answer_evidence"]["ids"]["raw_value"] == result["answers"]["ids"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("first_sql", ["SELECT DISTINCT score FROM people", "DELETE FROM people"])
@pytest.mark.parametrize("profile", ["sql-semantic", "sql-semantic-plan"])
def test_native_profiles_can_observe_before_contract_with_same_read_only_budget(
    database, first_sql, profile
):
    from langchain_core.messages import AIMessage, ToolMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "execute_sql" in names and "declare_unsupported" in names
            assert "crosscheck_sql" not in names
            assert ("submit_query_plan" in names) == (profile == "sql-semantic-plan")
            assert len(names) == len(kwargs["tools"])
            assert kwargs["tool_choice"] == "required" and not kwargs["parallel_tool_calls"]
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                name, args = "execute_sql", {"sql": first_sql}
            else:
                assert any(isinstance(message, ToolMessage) for message in messages)
                name, args = (
                    "submit_sql",
                    {
                        "sql": "SELECT COUNT(*) AS n FROM people",
                        "output_columns": ["n"],
                        "shape": "scalar",
                    },
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(self.calls)}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    before = database.read_bytes()
    task = Task("bird-mini-dev-sqlite", "synthetic", "How many people?", "", "easy", "people")
    result = BoundedSQLAgent(Native(), tool_profile=profile, max_steps=2).run(task, database)
    assert result["failure"] is None and "COUNT(*)" in result["final_sql"]
    assert len(result["trace"]) == 2 and result["tokens"] == 20
    assert result["trace"][0]["status"] == ("error" if first_sql.startswith("DELETE") else "ok")
    assert result["task_contract"]["answer"]["columns"] == ["n"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("empty_denominator", [False, True])
def test_population_plan_uses_full_final_guards_and_never_invents_zero(database, empty_denominator):
    question = (
        "What percentage of people with score greater than 99 have id 1?"
        if empty_denominator
        else "What percentage of people have id 1?"
    )
    plan = {
        "operation": "population_ratio",
        "relations": ["people"],
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
    }
    if empty_denominator:
        plan["filters"] = [
            {
                "field": {"relation": "people", "column": "score"},
                "operator": "gt",
                "value": 99,
                "source_excerpt": question,
            }
        ]
    before = database.read_bytes()
    result = run(
        database, [{"submit_query_plan": plan}], profile="sql-semantic-plan", question=question
    )
    if empty_denominator:
        assert result["failure"] and result["final_sql"] is None
        assert result["trace"][0]["checks"] == ["undefined_percentage_check_population"]
    else:
        from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

        assert result["failure"] is None
        assert SQLiteReadOnlyExecutor(database).execute(result["final_sql"]).rows == ((50.0,),)
        assert result["trace"][0]["compiled_query_plan"]["operation"] == "population_ratio"
        assert result["trace"][0]["result_hash"]
    assert database.read_bytes() == before


def test_plan_root_source_is_bound_but_fabricated_source_is_not_overwritten(database):
    question = "How many people?"
    plan = {
        "operation": "count",
        "relations": ["people"],
        "count_name": "n",
        "entity_keys": [{"relation": "people", "column": "id"}],
    }
    result = run(
        database,
        [
            {"submit_query_plan": plan | {"source_excerpt": "Question: " + question}},
            {"submit_query_plan": plan},
        ],
        profile="sql-semantic-plan",
        question=question,
    )
    assert result["failure"] is None
    assert result["trace"][0]["checks"] == ["plan_public_source"]
    assert result["trace"][1]["compiled_query_plan"]["source_excerpt"] == question
    assert result["task_contract"]["question"] == question


@pytest.mark.parametrize(
    "source,value,code",
    [
        ("private fabricated schema explanation", 1, "plan_public_source"),
        ("What percentage of people have id 1?", 9, "plan_filter_source"),
    ],
)
def test_plan_filter_source_and_literal_remain_strict_after_root_binding(
    database, source, value, code
):
    question = "What percentage of people have id 1?"
    plan = {
        "operation": "population_ratio",
        "relations": ["people"],
        "entity_keys": [{"relation": "people", "column": "id"}],
        "numerator_filters": [
            {
                "field": {"relation": "people", "column": "id"},
                "operator": "eq",
                "value": value,
                "source_excerpt": source,
            }
        ],
    }
    result = run(
        database, [{"submit_query_plan": plan}], profile="sql-semantic-plan", question=question
    )
    assert result["failure"] and result["final_sql"] is None
    assert result["trace"][0]["checks"] == [code]
    assert "private fabricated" not in json.dumps(result["trace"])


@pytest.mark.parametrize("with_contract", [False, True])
def test_native_plan_profile_has_a_typed_unsupported_exit_before_and_after_contract(
    database, with_contract
):
    from langchain_core.messages import AIMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            assert "declare_unsupported" in {tool["function"]["name"] for tool in kwargs["tools"]}
            return self

        def invoke(self, messages):
            self.calls += 1
            if with_contract and self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "plan_analysis",
                            "args": {"answer": {"columns": ["n"]}},
                            "id": "plan",
                        }
                    ],
                    usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "declare_unsupported",
                        "args": {"reason": "not certifiable"},
                        "id": "one",
                    }
                ],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    task = Task("bird-mini-dev-sqlite", "synthetic", "Unsupported SQL?", "", "hard", "people")
    result = BoundedSQLAgent(Native(), tool_profile="sql-semantic-plan", max_steps=2).run(
        task, database
    )
    assert result["failure"] == "unsupported_capability" and result["final_sql"] is None
    assert (result["task_contract"] is not None) == with_contract
    assert not result["answers"]


def test_native_binding_schema_uses_successful_steps_only_after_failed_computation(database):
    from langchain_core.messages import AIMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            tools = {t["function"]["name"]: t["function"] for t in kwargs["tools"]}
            if self.calls < 2:
                assert "submit_answers" not in tools
            else:
                field = tools["submit_answers"]["parameters"]["properties"]["fields"]["items"]
                assert field["properties"]["name"]["enum"] == ["n"]
                assert field["properties"]["step"]["enum"] == [2]
                assert "rule" not in field["properties"]
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                name, args = "compute_sql", {"sql": "SELECT absent FROM people"}
            elif self.calls == 2:
                name, args = "compute_sql", {"sql": "SELECT COUNT(*) AS n FROM people"}
            else:
                name, args = (
                    "submit_answers",
                    {"fields": [{"name": "n", "step": 2, "path": ["rows", 0, 0]}]},
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(self.calls)}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    task = Task(
        "infiagent-dabench",
        "synthetic",
        "Count people.",
        '@n[value] where "value" is an integer.',
        "easy",
        "people",
    )
    result = BoundedSQLAgent(Native(), tool_profile="sql-semantic", max_steps=3).run(task, database)
    assert result["failure"] is None and result["answers"] == {"n": 2}
    assert result["trace"][0]["code"] == "unknown_column"
    assert result["answer_evidence"]["n"]["step"] == 2


@pytest.mark.parametrize("unsupported", [False, True])
@pytest.mark.parametrize("with_documents", [False, True])
def test_native_collection_phase_defers_scalar_actions_and_keeps_typed_exit(
    database, unsupported, with_documents
):
    from langchain_core.messages import AIMessage

    class Native:
        calls = 0

        def bind(self, **kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert kwargs["tool_choice"] == "required" and not kwargs["parallel_tool_calls"]
            assert "submit_sql_answers" not in names
            if not self.calls and not with_documents:
                assert names == {"collect_values", "declare_unsupported"}
            else:
                assert {"compute_sql", "search_documents"} <= names
                assert ("submit_answers" in names) == bool(self.calls)
                if self.calls:
                    tool = next(
                        t for t in kwargs["tools"] if t["function"]["name"] == "submit_answers"
                    )
                    properties = tool["function"]["parameters"]["properties"]["fields"]["items"][
                        "properties"
                    ]
                    assert properties["name"]["enum"] == ["ids"]
                    assert properties["step"]["enum"] == [1]
                    assert "rule" not in properties
            return self

        def invoke(self, messages):
            self.calls += 1
            if unsupported:
                name, args = "declare_unsupported", {"reason": "unavailable computation"}
            elif self.calls == 1:
                name, args = (
                    "collect_values",
                    {"sql": "SELECT id FROM people ORDER BY id", "collection_kind": "list"},
                )
            else:
                name, args = (
                    "submit_answers",
                    {"fields": [{"name": "ids", "step": 1, "path": ["items"]}]},
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(self.calls)}],
                usage_metadata={"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
            )

    task = Task(
        "infiagent-dabench",
        "synthetic",
        "IDs?",
        '@ids[values] where "values" is a list of integers in ascending order.',
        "easy",
        "people",
    )
    before = database.read_bytes()
    model = Native()
    result = BoundedSQLAgent(model, tool_profile="sql-semantic", max_steps=2).run(
        task, database, documents="Public external rule document" if with_documents else ""
    )
    if unsupported:
        assert result["failure"] == "unsupported_capability" and not result["answers"]
    else:
        assert result["failure"] is None and result["answers"] == {"ids": [1, 2]}
        assert model.calls == 2
        assert result["answer_evidence"]["ids"]["step"] == 1
    assert database.read_bytes() == before
