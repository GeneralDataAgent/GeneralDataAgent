"""Repairs must remain evidence-driven and cannot turn unexecuted answers into success."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_repair import (
    bounded_observation,
    execution_hint,
    repair_action,
)


def test_combined_answer_is_discarded_and_only_sql_is_executed():
    action, reason = repair_action(
        {"sql": "SELECT 1", "answers": {"n": "999"}, "evidence_steps": [1]}
    )
    assert action == {"sql": "SELECT 1"}
    assert reason == "premature_answers_discarded"
    assert repair_action({"sql": "", "analysis": {"operation": "describe"}})[0] == {
        "analysis": {"operation": "describe"}
    }
    assert len(repair_action({"sql": "SELECT 1", "analysis": {"operation": "describe"}})[0]) == 2


def test_large_observation_is_bounded_without_mutating_actual_evidence():
    result = {"columns": ["payload"], "rows": [("a" * 8000,)] * 200}
    output = bounded_observation(result)
    assert len(json.dumps(output)) <= 6000
    assert output["truncated"] and output["total_rows"] == 200
    assert len(output["rows"]) < 200
    assert result["rows"][0][0] == "a" * 8000
    assert bounded_observation({"columns": ["n"], "rows": [[3]]}) == {
        "columns": ["n"],
        "rows": [[3]],
    }
    assert len(json.dumps(bounded_observation({"matches": [{"text": "a" * 20000}]}))) <= 6000
    assert bounded_observation({"columns": ["x" * 400] * 40, "rows": []})["columns_truncated"]
    for unusual in [
        {"columns": ["列" * 400] * 40, "rows": []},
        {"matches": [{"text": (chr(92) + chr(34)) * 20000}]},
        {"matches": [{"text": "汉字" * 20000}]},
    ]:
        assert len(json.dumps(bounded_observation(unusual))) <= 6000
    with pytest.raises(ValueError):
        bounded_observation(result, max_chars=100)


def test_only_known_execution_diagnostics_are_returned():
    assert "missing" in execution_hint(
        RuntimeError("read-only query failed: no such column: missing")
    )
    assert "Aggregate" in execution_hint(RuntimeError("query exceeded the 200 row limit"))
    assert "secret-token" not in execution_hint(RuntimeError("SDK failed secret-token"))
    assert "secret-token" not in execution_hint(RuntimeError("no such column: bad\nsecret-token"))


class Model:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = None

    def invoke(self, messages):
        self.messages = list(messages)
        return SimpleNamespace(
            content=json.dumps(next(self.actions)),
            usage_metadata={"total_tokens": 1},
            response_metadata={},
        )


def test_combined_action_requires_observed_sql_before_final_answer(tmp_path):
    db = tmp_path / "data.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE data(x REAL)")
        connection.executemany("INSERT INTO data VALUES (?)", [(2,), (4,)])
    model = Model(
        [
            {"sql": "SELECT SUM(x) AS n FROM data", "answers": {"n": "999"}},
            {"answers": {"n": "6"}, "evidence_steps": [1]},
        ]
    )
    task = Task("infiagent-dabench", "fixture", "sum?", "@n[value]", "easy", "data.csv")
    result = BoundedSQLAgent(model, tool_profile="sql-repair").run(task, db)
    assert result["failure"] is None and result["answers"] == {"n": "6"}
    assert any(event.get("code") == "premature_answers_discarded" for event in result["trace"])
    assert any(event.get("tool") == "sql" and event["status"] == "ok" for event in result["trace"])


def test_missing_evidence_is_repaired_but_never_accepted_unobserved(tmp_path):
    db = tmp_path / "data.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE data(x REAL)")
        connection.execute("INSERT INTO data VALUES (2)")
    task = Task("infiagent-dabench", "fixture", "sum?", "@n[value]", "easy", "data.csv")
    model = Model(
        [
            {"answers": {"n": "2"}},
            {"sql": "SELECT SUM(x) FROM data"},
            {"answers": {"n": "2"}, "evidence_steps": [2]},
        ]
    )
    result = BoundedSQLAgent(model, max_steps=3, tool_profile="sql-repair").run(task, db)
    assert result["failure"] is None
    bad = Model([{"answers": {"n": "2"}, "evidence_steps": [1]}] * 3)
    result = BoundedSQLAgent(bad, max_steps=3, tool_profile="sql-repair").run(task, db)
    assert result["failure"] == "missing_evidence" and result["answers"] == {}


def test_unknown_column_feedback_enables_a_valid_retry(tmp_path):
    db = tmp_path / "data.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE data(x REAL)")
    model = Model([{"sql": "SELECT unknown FROM data"}, {"final_sql": "SELECT COUNT(*) FROM data"}])
    task = Task("bird-mini-dev-sqlite", "fixture", "count?", "", "simple", "data")
    result = BoundedSQLAgent(model, tool_profile="sql-repair").run(task, db)
    assert result["failure"] is None
    assert any(
        "no such column: unknown" in message[1]
        for message in model.messages
        if message[0] == "human"
    )


def test_public_answer_keys_are_repaired_without_using_labels(tmp_path):
    db = tmp_path / "data.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE data(x REAL)")
        connection.execute("INSERT INTO data VALUES (2)")
    task = Task("infiagent-dabench", "fixture", "sum?", "@n[value]", "easy", "data.csv")
    model = Model(
        [
            {"sql": "SELECT SUM(x) FROM data"},
            {"answers": {"answer": "2"}, "evidence_steps": [1]},
            {"answers": {"n": "2"}, "evidence_steps": [1]},
        ]
    )
    result = BoundedSQLAgent(model, max_steps=3, tool_profile="sql-repair").run(task, db)
    assert result["failure"] is None and result["answers"] == {"n": "2"}
    assert any("Use every public answer field" in text for _, text in model.messages)
