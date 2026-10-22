import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.contracts.statistics import RegressionRequest
from generaldataagent.evaluation.stage_seven import bind_typed_tools
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task


def test_science_schema_is_opt_in():
    class Model:
        def bind(self, **kwargs):
            return kwargs

    def schema(science):
        result = bind_typed_tools(Model(), sql_output=False, science_enabled=science)
        return next(
            t["function"]["parameters"]
            for t in result["tools"]
            if t["function"]["name"] == "fixed_analysis"
        )

    legacy, science = schema(False), schema(True)
    assert "statistics" not in legacy["properties"]
    assert "regression_evaluation" not in legacy["properties"]["operation"]["enum"]
    assert "statistics" in science["properties"]
    assert "experiment" in science["properties"]
    assert "$ref" not in json.dumps(science)


@pytest.mark.parametrize("operation", ["statistics", "regression_evaluation"])
def test_agent_dispatch_and_field_evidence(tmp_path, operation):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table data (x REAL,z REAL,y REAL)")
        db.executemany(
            "insert into data values (?,?,?)",
            [(i, i * i % 13, 2 * i - 3 * (i * i % 13) + 7) for i in range(40)],
        )
    if operation == "statistics":
        arguments = {
            "table": "data",
            "columns": ["xz", "y"],
            "operation": operation,
            "statistics": {
                "columns": ["xz", "y"],
                "operation": "pearson",
                "features": [{"name": "xz", "operation": "multiply", "columns": ["x", "z"]}],
            },
        }
        binding = ["groups", 0, "correlation"]
    else:
        arguments = {
            "table": "data",
            "columns": ["x", "z", "y"],
            "operation": operation,
            "experiment": {"features": ["x", "z"], "target": "y"},
        }
        binding = ["metrics", "r2"]
    actions = iter(
        [
            {"analysis": arguments},
            {"answers": {}, "evidence_steps": [1], "bindings": {"answer": [1, binding]}},
        ]
    )

    class Model:
        def invoke(self, messages):
            assert "regression_evaluation" in messages[0][1]
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 10}
            )

    task = Task(
        "infiagent-dabench",
        "synthetic-stage8",
        "Compute the declared statistic",
        "@answer[float]",
        "easy",
        "demo.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-science").run(task, path)
    assert result["failure"] is None
    assert result["trace"][0]["tool"] == "fixed_analysis"
    assert "answer" in result["answers"]
    if operation == "regression_evaluation":
        assert float(result["answers"]["answer"]) == pytest.approx(1)


def test_predictive_derived_target_or_group_aggregates_are_rejected():
    for derived in [
        [{"name": "proxy", "operation": "multiply", "columns": ["y", "x"]}],
        [
            {"name": "intermediate", "operation": "add", "columns": ["y", "x"]},
            {"name": "proxy", "operation": "multiply", "columns": ["intermediate", "x"]},
        ],
        [{"name": "proxy", "operation": "group_mean", "columns": ["x"], "group_by": ["g"]}],
    ]:
        with pytest.raises(ValueError):
            RegressionRequest.model_validate(
                {"features": ["proxy"], "target": "y", "derived": derived}
            )
