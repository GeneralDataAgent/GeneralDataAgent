import pytest

from generaldataagent.tools.statistical_methods import public_ddof, standard_deviation_bindings


def test_public_ddof_defaults_are_explicit_and_conflicts_reject():
    assert public_ddof("Calculate the standard deviation for all countries") == 1
    assert public_ddof("population standard deviation") == 0
    assert public_ddof("sample standard deviation") == 1
    assert public_ddof("ddof=0") == 0
    with pytest.raises(ValueError, match="conflicting"):
        public_ddof("population standard deviation with ddof=1")


def test_sql_shortcut_cannot_certify_standard_deviation_method():
    text = "Calculate mean and standard deviation"
    bindings = {"mean": [1, ["rows", 0, 0]], "standard_deviation": [1, ["rows", 0, 1]]}
    assert not standard_deviation_bindings(bindings, {1: {"method_id": "sql"}}, text)
    bindings["standard_deviation"] = [2, ["statistics", "x", "sample_std"]]
    assert standard_deviation_bindings(
        bindings, {1: {"method_id": "sql"}, 2: {"method_id": "describe"}}, text
    )
    assert not standard_deviation_bindings(
        bindings, {2: {"method_id": "describe"}}, "population standard deviation"
    )
    summary = {2: {"method_id": "summary", "method_spec": {"ddof": 0}}}
    bindings["standard_deviation"] = [2, ["groups", 0, "statistics", "x", "std"]]
    assert standard_deviation_bindings(bindings, summary, "population standard deviation")
    assert not standard_deviation_bindings(bindings, summary, text)


def test_agent_rejects_manual_population_sql_then_uses_default_sample_method(tmp_path):
    import json
    import sqlite3
    from types import SimpleNamespace

    from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE data(x REAL)")
        db.executemany("INSERT INTO data VALUES (?)", [(1,), (2,), (3,)])
    actions = iter(
        [
            {"answer_sql": "SELECT SQRT(AVG(x*x)-AVG(x)*AVG(x)) AS sd FROM data"},
            {"analysis": {"table": "data", "columns": ["x"], "operation": "describe"}},
            {
                "answers": {},
                "evidence_steps": [2],
                "bindings": {"sd": [2, ["statistics", "x", "sample_std"]]},
            },
        ]
    )

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 10}
            )

    task = Task(
        "infiagent-dabench",
        "synthetic-std",
        "Calculate the standard deviation of x",
        "@sd[float]",
        "easy",
        "data.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-science").run(task, database)
    assert result["failure"] is None
    assert float(result["answers"]["sd"]) == 1
    assert result["trace"][0]["status"] == "error"
    assert "standard_deviation_requires_fixed_method_binding" in result["trace"][0]["checks"]
