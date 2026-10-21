"""Analytic finite-sample examples and method-bound independent group answers."""

import json
import math
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.contracts.analysis import AnalysisArguments
from generaldataagent.evaluation.stage_seven import bind_typed_tools
from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis

PUBLIC = (
    "Compare male members with an independent sample t-test at significance level 0.05. "
    'Labels are "different" and "same".'
)


def specification(variance="student"):
    return {
        "table": "data",
        "columns": ["value", "cohort"],
        "operation": "independent_t_test",
        "test": {
            "groups": ["A", "B"],
            "variance": variance,
            "filters": [{"column": "sex", "value": "male", "source_excerpt": "male members"}],
            "significance_rule": {
                "alpha": 0.05,
                "significant_label": "different",
                "nonsignificant_label": "same",
            },
        },
    }


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "groups.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(value,cohort,sex)")
        db.executemany(
            "INSERT INTO data VALUES (?,?,?)",
            [
                (0, "A", "male"),
                (2, "A", "male"),
                (2, "B", "male"),
                (4, "B", "male"),
                (999, "A", "female"),
                (None, "B", "male"),
                (float("inf"), "A", "male"),
                ("bad", "B", "male"),
            ],
        )
    return path


@pytest.mark.parametrize("variance", ["student", "welch"])
def test_exact_finite_sample_p_and_scoped_groups(database, variance):
    before = database.read_bytes()
    r = fixed_analysis(
        database, specification(variance), allowed_relations={"data"}, public_text=PUBLIC
    )
    # Analytic df=2 two-sided tail: 1-|t|/sqrt(t*t+2).
    assert r["statistic"] == pytest.approx(-math.sqrt(2))
    assert r["degrees_of_freedom"] == pytest.approx(2)
    assert r["p_value"] == pytest.approx(1 - 1 / math.sqrt(2))
    assert [g["mean"] for g in r["groups"]] == [1, 3]
    assert [g["sample_std"] for g in r["groups"]] == pytest.approx([math.sqrt(2)] * 2)
    assert [g["population_std"] for g in r["groups"]] == [1, 1]
    assert r["observations"] == 4 and r["dropped_rows"] == 3
    assert r["public_rule_label"] == "same"
    assert r["assumptions_verified"] is False and r["policy_interpretation_verified"] is False
    assert database.read_bytes() == before


def test_unequal_variance_welch_degrees_of_freedom(tmp_path):
    path = tmp_path / "unequal.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(value,cohort,sex)")
        db.executemany(
            "INSERT INTO data VALUES (?,?,'male')",
            [(v, "A") for v in [1, 2, 3]] + [(v, "B") for v in [0, 4, 8, 12]],
        )
    r = fixed_analysis(path, specification("welch"), allowed_relations={"data"}, public_text=PUBLIC)
    a, b = 1 / 3, (80 / 3) / 4
    assert r["degrees_of_freedom"] == pytest.approx((a + b) ** 2 / (a * a / 2 + b * b / 3))
    assert r["statistic"] == pytest.approx((2 - 6) / math.sqrt(a + b))
    assert 0 < r["p_value"] < 1


@pytest.mark.parametrize("mutation", ["scope", "column", "filter", "alpha", "label"])
def test_scope_and_fabricated_public_rules_rejected(database, mutation):
    spec = specification()
    allowed = {"data"}
    if mutation == "scope":
        allowed = {"other"}
    elif mutation == "column":
        spec["test"]["filters"][0]["column"] = "missing"
    elif mutation == "filter":
        spec["test"]["filters"][0]["value"] = "female"
    elif mutation == "alpha":
        spec["test"]["significance_rule"]["alpha"] = 0.01
    else:
        spec["test"]["significance_rule"]["significant_label"] = "invented"
    before = database.read_bytes()
    with pytest.raises(ValueError):
        fixed_analysis(database, spec, allowed_relations=allowed, public_text=PUBLIC)
    assert database.read_bytes() == before


@pytest.mark.parametrize("rows", [[(1, "A"), (2, "B")], [(1, "A"), (1, "A"), (2, "B"), (2, "B")]])
def test_insufficient_or_degenerate_groups_rejected(tmp_path, rows):
    path = tmp_path / "bad.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(value,cohort,sex)")
        db.executemany("INSERT INTO data VALUES (?,?,'male')", rows)
    with pytest.raises(ValueError):
        fixed_analysis(path, specification(), allowed_relations={"data"}, public_text=PUBLIC)


def test_typed_parameters_are_operation_specific():
    spec = specification()
    with pytest.raises(ValueError):
        AnalysisArguments.model_validate(spec | {"operation": "pearson"})
    for groups in [[1, 1.0], [True, False], [float("inf"), 0], [2**70, 0], ["a" * 201, "B"]]:
        spec["test"]["groups"] = groups
        with pytest.raises(ValueError):
            AnalysisArguments.model_validate(spec)


@pytest.mark.parametrize("variance,named", [("student", "Welch"), ("welch", "Student's")])
def test_public_named_variance_method_cannot_be_replaced(database, variance, named):
    with pytest.raises(ValueError, match="named test"):
        fixed_analysis(
            database,
            specification(variance),
            allowed_relations={"data"},
            public_text=PUBLIC + " Use " + named + " t-test.",
        )


def test_public_named_alpha_cannot_borrow_an_unrelated_threshold(database):
    spec = specification()
    spec["test"]["significance_rule"]["alpha"] = 0.5
    with pytest.raises(ValueError, match="named level"):
        fixed_analysis(
            database,
            spec,
            allowed_relations={"data"},
            public_text=PUBLIC + " Another threshold is 0.5.",
        )


def test_native_test_schema_is_requested_and_self_contained():
    class Model:
        def bind(self, **kwargs):
            return kwargs

    for requested in [False, True]:
        bundle = bind_typed_tools(Model(), sql_output=False, independent_test_requested=requested)
        tools = {t["function"]["name"]: t["function"]["parameters"] for t in bundle["tools"]}
        props = tools["fixed_analysis"]["properties"]
        assert ("independent_t_test" in props["operation"]["enum"]) is requested
        assert ("test" in props) is requested
        assert ("submit_sql_answers" not in tools) is requested
        if requested:
            assert '"$ref"' not in json.dumps(props["test"])


def test_agent_rejects_approximation_and_binds_actual_test(tmp_path):
    path = tmp_path / "agent.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(value,cohort,sex)")
        db.executemany(
            "INSERT INTO data VALUES (?,?,'male')", [(0, "A"), (2, "A"), (2, "B"), (4, "B")]
        )
    actions = iter(
        [
            {
                "answer_sql": "SELECT AVG(value) AS a_mean, AVG(value) AS b_mean, "
                "CASE WHEN AVG(value)>1.96 THEN 'same' ELSE 'different' END AS decision FROM data"
            },
            {"analysis": specification()},
            {
                "answers": {},
                "evidence_steps": [2],
                "bindings": {
                    "a_mean": [2, ["groups", 0, "mean"]],
                    "b_mean": [2, ["groups", 1, "mean"]],
                    "decision": [2, ["public_rule_label"]],
                },
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
        "synthetic",
        PUBLIC,
        "@a_mean[first] @b_mean[second] @decision[outcome]. "
        'Where "first" is a number rounded to two decimal places. '
        '"second" is a number rounded to two decimal places. "outcome" is a string.',
        "easy",
        "data.csv",
    )
    r = BoundedSQLAgent(Model(), tool_profile="sql-semantic").run(task, path)
    assert r["failure"] is None
    assert r["trace"][0]["checks"] == ["independent_test_requires_method_bindings"]
    assert r["answers"] == {"a_mean": "1.00", "b_mean": "3.00", "decision": "same"}
    assert {v["method_id"] for v in r["answer_evidence"].values()} == {"independent_t_test"}
    assert (
        answer_receipt_errors(
            {"benchmark": task.benchmark, "results": [r | {"case_id": "synthetic", "passed": True}]}
        )
        == []
    )
