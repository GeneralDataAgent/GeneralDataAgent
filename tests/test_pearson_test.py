"""Independent numerical examples and provenance boundaries for the fixed test."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.contracts.analysis import AnalysisArguments
from generaldataagent.contracts.statistical_rules import PearsonDecisionRule
from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.tools.pearson_test import pearson_test, public_decision

PUBLIC = (
    "Use alpha 0.05 and absolute r threshold 0.5. Labels are "
    '"strong", "weak", "absent". p<alpha is significant; abs(r)>=threshold is high.'
)
RULE = PearsonDecisionRule(
    alpha=0.05,
    abs_r_threshold=0.5,
    significant_high_label="strong",
    significant_low_label="weak",
    nonsignificant_label="absent",
)


@pytest.mark.parametrize(
    "rows,r,p",
    [
        ([(0, 0), (1, 2), (2, 1)], 0.5, 2 / 3),
        ([(-1, 0), (0, -1), (0, 1), (1, 0)], 0, 1),
        ([(0, 2), (1, 1)], -1, 1),
        ([(0, 0), (1, 1), (2, 2), (3, 3)], 1, 0),
        ([(0, 0), (1, -1), (2, -2), (3, -3)], -1, 0),
    ],
)
def test_known_exact_null_examples(rows, r, p):
    result = pearson_test(rows)
    assert result["correlation"] == pytest.approx(r)
    assert result["p_value"] == pytest.approx(p, abs=1e-14)
    assert result["alternative"] == "two-sided"
    assert result["assumptions_verified"] is False
    assert result["scipy_version"]


@pytest.mark.parametrize(
    "rows", [[], [(1, 2)], [(1, 2), (1, 3), (1, 4)], [(1, 2), (1 + 1e-15, 3), (1 - 1e-15, 4)]]
)
def test_insufficient_constant_and_unstable_inputs_rejected(rows):
    with pytest.raises(ValueError):
        pearson_test(rows)


def test_public_policy_uses_unrounded_values_and_explicit_boundaries():
    assert public_decision(RULE, 0.49999, 0.049999, PUBLIC)["public_rule_label"] == "weak"
    assert public_decision(RULE, -0.5, 0.049999, PUBLIC)["public_rule_label"] == "strong"
    result = public_decision(RULE, 0.9, 0.05, PUBLIC)
    assert result["public_rule_label"] == "absent"
    assert result["policy_interpretation_verified"] is False
    assert public_decision(RULE, 0.9, 0.050001, PUBLIC)["public_rule_label"] == "absent"
    with pytest.raises(ValueError, match="thresholds"):
        public_decision(RULE.model_copy(update={"alpha": 0.01}), 0.9, 0, PUBLIC)
    with pytest.raises(ValueError, match="named alpha"):
        public_decision(RULE.model_copy(update={"alpha": 0.5}), 0.9, 0, PUBLIC)
    with pytest.raises(ValueError, match="labels"):
        public_decision(
            RULE.model_copy(update={"nonsignificant_label": "invented"}), 0.9, 1, PUBLIC
        )
    with pytest.raises(ValueError):
        public_decision(RULE, 0.9, 0, "")
    with pytest.raises(ValueError):
        AnalysisArguments(table="data", columns=["x", "y"], operation="pearson", decision_rule=RULE)


def test_fixed_test_filters_pairs_keeps_database_readonly_and_enforces_scope(tmp_path):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE data(x,y)")
        db.executemany(
            "INSERT INTO data VALUES (?,?)",
            [(0, 0), (1, 2), (2, 1), (None, 1), (float("inf"), 1), (1, "bad")],
        )
    before = database.read_bytes()
    spec = {"table": "data", "columns": ["x", "y"], "operation": "pearson_test"}
    result = fixed_analysis(database, spec, allowed_relations={"data"})
    assert result["p_value"] == pytest.approx(2 / 3)
    assert result["observations"] == 3 and result["dropped_rows"] == 3
    with pytest.raises(ValueError, match="scope"):
        fixed_analysis(database, spec, allowed_relations={"other"})
    with pytest.raises(ValueError, match="unknown analysis column"):
        fixed_analysis(database, spec | {"columns": ["42", "y"]}, allowed_relations={"data"})
    assert database.read_bytes() == before


def test_real_agent_binds_r_p_and_public_policy_with_method_receipts(tmp_path):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE data(x,y)")
        db.executemany("INSERT INTO data VALUES (?,?)", [(0, 0), (1, 2), (2, 1)])
    actions = iter(
        [
            {
                "analysis": {
                    "table": "data",
                    "columns": ["x", "y"],
                    "operation": "pearson_test",
                    "decision_rule": RULE.model_dump(),
                }
            },
            {
                "answers": {},
                "evidence_steps": [1],
                "bindings": {
                    "r": [1, ["correlation"]],
                    "p": [1, ["p_value"]],
                    "label": [1, ["public_rule_label"]],
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
        "@r[coefficient], @p[probability], @label[relationship]. "
        'Where "coefficient" is a float rounded to two decimal places. '
        '"probability" is a float rounded to four decimal places. '
        '"relationship" is a string that can either be "strong", "weak", or "absent".',
        "easy",
        "data.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic").run(task, database)
    assert result["failure"] is None
    assert float(result["answers"]["r"]) == 0.5
    assert float(result["answers"]["p"]) == pytest.approx(0.6667)
    assert result["answers"]["label"] == "absent"
    report = {
        "benchmark": task.benchmark,
        "results": [result | {"case_id": "synthetic", "passed": True}],
    }
    assert answer_receipt_errors(report) == []
