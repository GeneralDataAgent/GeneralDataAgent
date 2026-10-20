"""SQL specialization exposes only declared schema and preserves scoped execution limits."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task, digest
from generaldataagent.evaluation.stage_six_sql_precision import SQL_PRECISION_PROMPT


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "keys.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE customer(id INTEGER PRIMARY KEY)")
        db.execute(
            "CREATE TABLE purchase(id INTEGER PRIMARY KEY,"
            "customer_id INTEGER REFERENCES customer(id))"
        )
        db.executemany("INSERT INTO customer VALUES (?)", [(1,), (2,)])
        db.executemany("INSERT INTO purchase VALUES (?,?)", [(1, 1), (2, 1), (3, 2)])
    return path


class Model:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = 0
        self.messages = []

    def invoke(self, messages):
        self.calls += 1
        self.messages = list(messages)
        return SimpleNamespace(
            content=json.dumps(next(self.actions)),
            usage_metadata={"total_tokens": 2},
            response_metadata={},
        )


def task(benchmark="bird-mini-dev-sqlite"):
    return Task(benchmark, "fixture", "How many customers have purchases?", "", "simple", "fixture")


def test_precision_exposes_real_keys_and_no_generic_answer_contract(database):
    before = digest(database)
    model = Model([{"sql": "", "final_sql": "SELECT COUNT(DISTINCT customer_id) FROM purchase"}])
    result = BoundedSQLAgent(model, tool_profile="sql-precision").run(task(), database)
    assert result["failure"] is None and result["final_sql"]
    assert model.messages[0] == ("system", SQL_PRECISION_PROMPT)
    request = json.loads(model.messages[1][1])
    assert "answer_fields" not in request and request["required_output"] == "final_sql"
    assert request["relationships"]["primary_keys"]["customer"] == ["id"]
    assert request["relationships"]["foreign_keys"][0]["references_column"] == "id"
    assert str(database) not in model.messages[1][1] and digest(database) == before


def test_precision_rejects_unavailable_tools_and_gives_sql_only_error_feedback(database):
    model = Model(
        [
            {"analysis": {"table": "purchase", "columns": ["id"], "operation": "describe"}},
            {"sql": "SELECT nonexistent FROM purchase"},
            {"final_sql": "SELECT COUNT(DISTINCT customer_id) FROM purchase"},
        ]
    )
    result = BoundedSQLAgent(model, max_steps=3, tool_profile="sql-precision").run(task(), database)
    assert result["failure"] is None
    assert result["trace"][0]["code"] == "invalid_action"
    feedback = [json.loads(text) for role, text in model.messages if role == "human"]
    hints = [r["hint"] for r in feedback if "hint" in r]
    assert hints and "nonexistent" in hints[0] and "fixed analysis" not in hints[0]
    assert all(event.get("tool") != "fixed_analysis" for event in result["trace"])


def test_precision_keeps_read_only_scope_and_rejects_non_sql_tasks(database):
    before = digest(database)
    model = Model([{"final_sql": "DELETE FROM purchase"}])
    result = BoundedSQLAgent(model, max_steps=1, tool_profile="sql-precision").run(task(), database)
    assert result["failure"] == "execution_or_model_error" and digest(database) == before
    fresh = Model([])
    with pytest.raises(ValueError, match="SQL-output"):
        BoundedSQLAgent(fresh, tool_profile="sql-precision").run(
            task("infiagent-dabench"), database
        )
    assert fresh.calls == 0
