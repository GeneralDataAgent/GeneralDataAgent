"""Independent numerical examples and provenance boundaries for the fixed test."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.contracts.analysis import AnalysisArguments
from generaldataagent.contracts.independent_test import SignificanceRule
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


def test_binary_p_policy_uses_unrounded_p_without_a_strength_threshold():
    from generaldataagent.tools.pearson_test import public_significance

    rule = SignificanceRule(alpha=0.05, significant_label="sig", nonsignificant_label="none")
    text = 'At alpha 0.05, labels are "sig" and "none".'
    assert public_significance(rule, 0.049999, text)["public_rule_label"] == "sig"
    assert public_significance(rule, 0.05, text)["public_rule_label"] == "none"
    for rows, label in [([(0, 0), (1, 1), (2, 2)], "sig"), ([(0, 0), (1, 2)], "none")]:
        result = pearson_test(rows, significance_rule=rule, public_text=text)
        assert result["public_rule_label"] == label
        assert result["policy_interpretation_verified"] is False
        assert result["assumptions_verified"] is False
        assert "abs_r_threshold" not in result["significance_rule"]
        assert result["decision_source_sha256"]
    for text in [
        "",
        'alpha 0.01; unrelated value 0.05; labels "sig" and "none"',
        "alpha 0.05, unquoted sig and none",
    ]:
        with pytest.raises(ValueError):
            pearson_test([(0, 0), (1, 1), (2, 2)], significance_rule=rule, public_text=text)
    with pytest.raises(ValueError, match="one public"):
        pearson_test([(0, 0), (1, 1)], rule=RULE, significance_rule=rule)
    with pytest.raises(ValueError, match="one public"):
        AnalysisArguments(
            table="data",
            columns=["x", "y"],
            operation="pearson_test",
            decision_rule=RULE,
            significance_rule=rule,
        )
    with pytest.raises(ValueError, match="requires pearson_test"):
        AnalysisArguments(
            table="data", columns=["x", "y"], operation="pearson", significance_rule=rule
        )


def test_binary_policy_has_native_schema_and_end_to_end_evidence_binding(tmp_path):
    from generaldataagent.evaluation.stage_seven import bind_typed_tools

    class BoundModel:
        def bind(self, **kwargs):
            return kwargs

    tools = bind_typed_tools(BoundModel(), sql_output=False)["tools"]
    schema = next(
        t["function"]["parameters"] for t in tools if t["function"]["name"] == "fixed_analysis"
    )
    assert schema["properties"]["significance_rule"]["required"] == [
        "alpha",
        "significant_label",
        "nonsignificant_label",
    ]
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE data(x REAL,y REAL); INSERT INTO data VALUES(0,0),(1,2),(2,1);"
        )
    actions = iter(
        [
            {
                "analysis": {
                    "table": "data",
                    "columns": ["x", "y"],
                    "operation": "pearson_test",
                    "significance_rule": {
                        "alpha": 0.05,
                        "significant_label": "sig",
                        "nonsignificant_label": "none",
                    },
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
                content=json.dumps(next(actions)),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    text = (
        'Use alpha 0.05, labels "sig" and "none". @r[r_value] @p[p_value] @label[label_value] '
        'where "r_value" is a number rounded to two decimal places. '
        '"p_value" is a number rounded to four decimal places. "label_value" is a string.'
    )
    before = database.read_bytes()
    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(
        Task(
            "infiagent-dabench",
            "synthetic",
            "Two-sided Pearson and significance?",
            text,
            "easy",
            "data",
        ),
        database,
    )
    assert result["failure"] is None
    assert result["answers"] == {"r": "0.50", "p": "0.6667", "label": "none"}
    assert {v["method_id"] for v in result["answer_evidence"].values()} == {"pearson_test"}
    assert (
        answer_receipt_errors(
            {
                "benchmark": "infiagent-dabench",
                "results": [result | {"case_id": "synthetic", "passed": True}],
            }
        )
        == []
    )
    assert database.read_bytes() == before


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


def test_absolute_r_binary_policy_boundary_uses_unrounded_magnitude_not_p_value():
    from generaldataagent.contracts.statistical_rules import PearsonMagnitudeRule
    from generaldataagent.tools.pearson_test import public_magnitude

    rule = PearsonMagnitudeRule(abs_r_threshold=0.1, above_label="correlate", below_label="none")
    text = 'The absolute value of r is greater than or equal to 0.1; "correlate" or "none".'
    for r, expected in [(0.1, "correlate"), (-0.1, "correlate"), (0.099999, "none"), (0, "none")]:
        assert public_magnitude(rule, r, text)["public_rule_label"] == expected
    # Two observations have exact p=1: this public magnitude rule must still say correlate.
    result = pearson_test([(0, 1), (1, 0)], magnitude_rule=rule, public_text=text)
    assert result["p_value"] == 1
    assert result["public_rule_label"] == "correlate"
    assert result["policy_interpretation_verified"] is False
    assert "p-value significance" in result["decision_scope"]
    strict = rule.model_copy(update={"comparison": "gt"})
    assert (
        public_magnitude(strict, 0.1, 'abs(r) > 0.1; "correlate", "none"')["public_rule_label"]
        == "none"
    )
    for invalid in [
        'alpha 0.1; "correlate", "none"',
        '|r| > 0.1; "correlate", "none"',
        '|r| >= 0.2; unrelated 0.1; "correlate", "none"',
        '|r| >= 0.1 and |r| >= 0.2; "correlate", "none"',
        "|r| >= 0.1; unquoted correlate, none",
    ]:
        with pytest.raises(ValueError):
            public_magnitude(rule, 0.5, invalid)
    with pytest.raises(ValueError, match="distinct"):
        PearsonMagnitudeRule(abs_r_threshold=0.1, above_label="same", below_label="same")
    with pytest.raises(ValueError, match="one public"):
        pearson_test([(0, 1), (1, 0)], rule=RULE, magnitude_rule=rule, public_text=text)
    with pytest.raises(ValueError, match="one public"):
        AnalysisArguments(
            table="data",
            columns=["x", "y"],
            operation="pearson_test",
            decision_rule=RULE,
            magnitude_rule=rule,
        )
    with pytest.raises(ValueError, match="requires pearson_test"):
        AnalysisArguments(
            table="data", columns=["x", "y"], operation="pearson", magnitude_rule=rule
        )


def test_magnitude_policy_fixed_analysis_and_receipts(tmp_path):
    from generaldataagent.evaluation.stage_seven import bind_typed_tools

    class BoundModel:
        def bind(self, **kwargs):
            return kwargs

    tools = bind_typed_tools(BoundModel(), sql_output=False)["tools"]
    schema = next(
        t["function"]["parameters"] for t in tools if t["function"]["name"] == "fixed_analysis"
    )
    assert "magnitude_rule" in schema["properties"]
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE data(x REAL,y REAL); INSERT INTO data VALUES(0,1),(1,0);")
    text = 'Use abs(r) >= 0.1, "correlate" or "none". @r[r_value] @label[label_value]'
    actions = iter(
        [
            {
                "analysis": {
                    "table": "data",
                    "columns": ["x", "y"],
                    "operation": "pearson_test",
                    "magnitude_rule": {
                        "abs_r_threshold": 0.1,
                        "above_label": "correlate",
                        "below_label": "none",
                    },
                }
            },
            {
                "answers": {},
                "evidence_steps": [1],
                "bindings": {"r": [1, ["correlation"]], "label": [1, ["public_rule_label"]]},
            },
        ]
    )

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)),
                usage_metadata={"total_tokens": 10},
                response_metadata={},
            )

    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(
        Task("infiagent-dabench", "synthetic", "Pearson relation?", text, "easy", "data"), database
    )
    assert result["failure"] is None
    assert result["answers"] == {"r": -1.0, "label": "correlate"}
    assert {v["method_id"] for v in result["answer_evidence"].values()} == {"pearson_test"}
    assert (
        answer_receipt_errors(
            {
                "benchmark": "infiagent-dabench",
                "results": [result | {"case_id": "synthetic", "passed": True}],
            }
        )
        == []
    )
