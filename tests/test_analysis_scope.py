"""Bad statistical identifiers fail before a data scan, with scoped repair hints."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.tools.analysis_scope import AnalysisColumnError, validate_analysis_columns
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "scoped.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE data(measure REAL, cohort INT, "group label" TEXT);'
            "INSERT INTO data VALUES(1,0,'a'),(2,0,'a'),(3,1,'b'),(5,1,'b');"
            "CREATE TABLE private(secret_measure REAL);"
        )
    return path


@pytest.mark.parametrize("operation", ["describe", "pearson_test", "independent_t_test"])
def test_unknown_fields_reject_before_executor_scan(database, monkeypatch, operation):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid field must be rejected before scanning observations")

    monkeypatch.setattr(SQLiteReadOnlyExecutor, "execute", forbidden)
    spec = {"table": "data", "columns": ["measur", "cohort"], "operation": operation}
    if operation == "independent_t_test":
        spec["test"] = {"groups": [0, 1], "variance": "welch"}
    with pytest.raises(AnalysisColumnError) as caught:
        fixed_analysis(database, spec, allowed_relations={"data"})
    assert "measure" in caught.value.candidates
    assert "secret_measure" not in caught.value.candidates
    assert len(caught.value.candidates) <= 5


def test_filter_fields_are_checked_and_unauthorized_tables_reveal_no_candidates(database):
    with pytest.raises(AnalysisColumnError) as caught:
        fixed_analysis(
            database,
            {
                "table": "data",
                "columns": ["measure", "cohort"],
                "operation": "independent_t_test",
                "test": {
                    "groups": [0, 1],
                    "variance": "student",
                    "filters": [{"column": "group labe", "value": "a", "source_excerpt": "a"}],
                },
            },
            allowed_relations={"data"},
            public_text="a",
        )
    assert "group label" in caught.value.candidates
    with pytest.raises(ValueError, match="scope"):
        validate_analysis_columns(database, "private", ["missing"], allowed_relations={"data"})
    validate_analysis_columns(
        database, "data", ["measure", "group label"], allowed_relations={"data"}
    )


def test_agent_can_correct_statistical_field_from_scoped_feedback(database):
    actions = iter(
        [
            {"analysis": {"table": "data", "columns": ["measur"], "operation": "describe"}},
            {"analysis": {"table": "data", "columns": ["measure"], "operation": "describe"}},
            {
                "answers": {},
                "evidence_steps": [2],
                "bindings": {"mean": [2, ["statistics", "measure", "mean"]]},
            },
        ]
    )
    feedback = []

    class Model:
        def invoke(self, messages):
            if len(feedback) == 1:
                error = json.loads(messages[-2][1])
                assert error["code"] == "unknown_column"
                assert "measure" in error["field_candidates"]
            feedback.append(True)
            return SimpleNamespace(
                content=json.dumps(next(actions)),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    before = database.read_bytes()
    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=3).run(
        Task("infiagent-dabench", "synthetic", "Mean?", "@mean[value]", "easy", "data"), database
    )
    assert result["failure"] is None and result["answers"] == {"mean": 2.75}
    assert result["trace"][0]["code"] == "unknown_column"
    assert database.read_bytes() == before
