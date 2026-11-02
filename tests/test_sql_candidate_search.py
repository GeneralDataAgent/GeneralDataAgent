"""Candidate search keeps execution scope, full results and whole-task budgets."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.orchestration.sql_search import (
    SearchBudgetExceeded,
    SearchModelBudget,
    select_sql_candidates,
)
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard


class Model:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        return SimpleNamespace(
            content=json.dumps(action),
            usage_metadata={"total_tokens": 10, "input_tokens": 7, "output_tokens": 3},
            response_metadata={},
        )


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE orders(id INTEGER, amount REAL);"
            "INSERT INTO orders VALUES (1,10),(2,20),(3,20);"
            "CREATE TABLE private_secret(secret TEXT);"
            "INSERT INTO private_secret VALUES ('hidden');"
        )
    return path


def select(database, candidates, actions=()):
    model = Model(actions)
    guard = SqlGuard(dialect="sqlite")
    executor = SQLiteReadOnlyExecutor(database, max_rows=100)

    def execute(sql):
        guard.inspect(sql, allowed_relations={"orders"})
        return executor.execute(sql)

    result = select_sql_candidates(
        question="What is the total amount?",
        instructions="Return one total",
        schema={"orders": ["id", "amount"]},
        candidates=candidates,
        execute=execute,
        model=model,
    )
    return result, model


def test_tournament_uses_execution_results_without_answers(database):
    result, model = select(
        database,
        ["SELECT COUNT(*) FROM orders", "SELECT SUM(amount) FROM orders", "SELECT 10"],
        [{"winner": 1}, {"winner": 0}, {"winner": 0}],
    )
    assert result["selected"] == 1
    assert len(result["votes"]) == len(model.messages) == 3
    payload = json.loads(model.messages[0][1][1])
    assert payload["candidates"][1]["preview"] == [[50.0]]
    assert set(payload) == {"question", "instructions", "schema", "candidates"}


def test_same_full_results_skip_judging_but_preserve_duplicates(database):
    result, model = select(
        database,
        ["SELECT amount FROM orders", "SELECT amount FROM orders ORDER BY amount DESC"],
    )
    assert result["groups"] == [[0, 1]] and not model.messages
    different, _ = select(
        database,
        ["SELECT amount FROM orders", "SELECT DISTINCT amount FROM orders"],
        [{"winner": 0}],
    )
    assert len(different["groups"]) == 2


@pytest.mark.parametrize(
    "first,second",
    [
        ("SELECT NULL", "SELECT 'None'"),
        ("SELECT 1,2", "SELECT 2,1"),
        ("SELECT 1 WHERE 0", "SELECT 1,2 WHERE 0"),
        ("SELECT x'31'", "SELECT '31'"),
    ],
)
def test_grouping_retains_nulls_width_column_order_and_types(database, first, second):
    result, _ = select(database, [first, second], [{"winner": 1}])
    assert len(result["groups"]) == 2 and result["selected"] == 1


def test_scope_and_writes_rejected_before_judging(database):
    before = database.read_bytes()
    result, model = select(
        database,
        ["SELECT * FROM private_secret", "DELETE FROM orders", "SELECT SUM(amount) FROM orders"],
    )
    assert [r["status"] for r in result["ledger"]] == ["rejected", "rejected", "executed"]
    assert result["selected"] == 2 and not model.messages
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "decision", [{"winner": True}, {"winner": 2}, {"winner": 0, "sql": "x"}, []]
)
def test_selector_cannot_invent_sql_or_out_of_range_winner(database, decision):
    with pytest.raises(ValueError, match="selector"):
        select(database, ["SELECT 1", "SELECT 2"], [decision])


def test_meter_counts_errors_and_unknown_cost():
    model = Model([{"winner": 0}, RuntimeError("service unavailable")])
    meter = SearchModelBudget(model, max_calls=2, max_seconds=10)
    meter.invoke([])
    with pytest.raises(RuntimeError, match="service"):
        meter.invoke([])
    with pytest.raises(SearchBudgetExceeded, match="step_budget"):
        meter.invoke([])
    assert len(model.messages) == 2
    assert meter.telemetry()["tokens"] == 10
    assert meter.telemetry()["cost_usd"] is None
    assert meter.telemetry()["usage_status"] == "partial_or_unknown"


def test_meter_discards_token_overshoot_response():
    model = Model([{"winner": 0}])
    meter = SearchModelBudget(model, max_calls=2, max_seconds=10, max_tokens=9)
    with pytest.raises(SearchBudgetExceeded, match="token_budget"):
        meter.invoke([])
    assert meter.tokens == 10 and meter.calls == 1
    with pytest.raises(SearchBudgetExceeded, match="token_budget"):
        meter.invoke([])
    assert len(model.messages) == 1


def test_complete_agent_adapter_uses_same_guards_and_combines_all_usage(database):
    model = Model(
        [
            {"final_sql": "SELECT COUNT(*) FROM orders"},
            {"final_sql": "SELECT SUM(amount) FROM orders"},
            {"final_sql": "SELECT MIN(amount) FROM orders"},
            {"winner": 1},
            {"winner": 0},
            {"winner": 0},
        ]
    )
    task = Task(
        "bird-mini-dev-sqlite", "synthetic", "What is the total amount?", "", "easy", "shop"
    )
    result = BoundedSQLAgent(
        model,
        tool_profile="sql-semantic-json",
        max_steps=12,
        sql_search="diverse-tournament-v1",
    ).run(task, database)
    assert result["failure"] is None
    assert result["final_sql"] == "SELECT SUM(amount) FROM orders"
    assert result["tokens"] == 60 and result["model_calls"] == 6
    assert result["cost_usd"] is None
    assert result["sql_search"]["selection"]["selected"] == 1
    assert len({messages[0][1] for messages in model.messages[:3]}) == 3


def test_agent_does_not_hide_provider_failure(database):
    model = Model([{"final_sql": "SELECT 1"}, RuntimeError("offline")])
    result = BoundedSQLAgent(
        model,
        tool_profile="sql-semantic-json",
        max_steps=12,
        sql_search="diverse-tournament-v1",
    ).run(Task("bird-mini-dev-sqlite", "s", "Total?", "", "easy", "shop"), database)
    assert result["failure"] == "model_error" and result["final_sql"] is None
    assert result["model_calls"] == 2 and result["usage_status"] == "partial_or_unknown"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": 6},
        {"tool_profile": "sql"},
        {"experience": lambda *args: []},
    ],
)
def test_invalid_search_assembly_refused(kwargs):
    configuration = {"tool_profile": "sql-semantic-json", "max_steps": 12, **kwargs}
    with pytest.raises(ValueError, match="SQL search"):
        BoundedSQLAgent(Model([]), sql_search="diverse-tournament-v1", **configuration)
