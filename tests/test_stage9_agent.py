import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from generaldataagent.business.rules import parse_band
from generaldataagent.contracts.analysis import AnswerSpec
from generaldataagent.evaluation.stage_nine import analyze_fee_rules
from generaldataagent.evaluation.stage_seven import bind_typed_tools
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.tools.sql_semantics import SemanticError, assemble_answers

DOCUMENT = """# Fees

If a field is set to null it means that it applies to all possible values of that field.
fee = fixed_amount + rate * transaction_value / 10000
"""
EXCERPT = DOCUMENT.splitlines()[2]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "payments.sqlite"
    annex = [
        dict(
            ID=1,
            card_scheme="Alpha",
            account_type=[],
            capture_delay=None,
            monthly_fraud_level=None,
            monthly_volume=None,
            merchant_category_code=[],
            is_credit=True,
            aci=None,
            intracountry=None,
            fixed_amount=0.05,
            rate=14,
        )
    ]
    merchant = dict(
        merchant="Shop", account_type="R", merchant_category_code=5411, capture_delay="1"
    )
    with sqlite3.connect(path) as db:
        for table, values in [("fees", annex), ("merchant_data", [merchant])]:
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            db.executemany(f"INSERT INTO {table} VALUES (?)", [(json.dumps(v),) for v in values])
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.executemany(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("Shop", 2023, 1, 100, "True", "Alpha", "B", "True", "NL", "NL"),
                ("Shop", 2023, 10, 300, "False", "Alpha", "B", "True", "NL", "BE"),
                ("Shop", 2023, 32, 900, "False", "Alpha", "B", "True", "NL", "NL"),
            ],
        )
    return path


def arguments(operation="average_fee", **extra):
    return dict(operation=operation, empty_list_is_wildcard=True, semantics_excerpt=EXCERPT) | extra


def execute(database, request):
    return analyze_fee_rules(
        database,
        request,
        allowed_relations={"fees", "merchant_data", "payments"},
        documents=DOCUMENT,
    )


@pytest.mark.parametrize("fault", ["coefficients_and_fee", "average", "truncated"])
def test_evaluation_fee_verification_uses_authoritative_rules(database, monkeypatch, fault):
    from generaldataagent.business.rules.application import FeeComputation
    from generaldataagent.evaluation import stage_nine

    original = stage_nine.compute_fee_summary

    def corrupted(rules, *, transaction_value):
        result = original(rules, transaction_value=transaction_value)
        if fault == "average":
            return FeeComputation(result.fees, Decimal("999"))
        if fault == "truncated":
            return FeeComputation((), None)
        forged = tuple(
            fee.model_copy(update={"fixed_amount": "1", "rate": "100", "fee": "1.1"})
            for fee in result.fees
        )
        return FeeComputation(forged, Decimal("1.1"))

    monkeypatch.setattr(stage_nine, "compute_fee_summary", corrupted)
    with pytest.raises(ValueError, match="fee arithmetic verification failed"):
        execute(database, arguments(transaction_value="10"))


@pytest.mark.parametrize(
    "change",
    [
        {"baseline_fee": "999", "alternate_fee": "999.06"},
        {"baseline_fee": "NaN"},
        {"alternate_fee": "999"},
        {"transaction_count": 999},
        {"transaction_count": 0},
        {"rule_id": "forged"},
        {"schema_version": "unsupported"},
    ],
)
def test_evaluation_counterfactual_checks_every_field_before_delivery(
    database, monkeypatch, change
):
    from generaldataagent.evaluation import stage_nine

    original = stage_nine.counterfactual_delta

    def corrupted(*args, **kwargs):
        # Keep the correct delta: checking only delta must not clear this claim.
        return original(*args, **kwargs).model_copy(update=change)

    monkeypatch.setattr(stage_nine, "counterfactual_delta", corrupted)
    with pytest.raises(ValueError, match="counterfactual independent delta verification failed"):
        execute(
            database,
            arguments(
                "counterfactual",
                merchant="Shop",
                year=2023,
                day_of_year=1,
                rule_id="1",
                alternate_rate="20",
            ),
        )


def test_rules_native_schema_is_opt_in():
    class Model:
        def bind(self, **kwargs):
            return kwargs

    for enabled in (False, True):
        tools = bind_typed_tools(Model(), sql_output=False, rules_enabled=enabled)["tools"]
        assert ("analyze_fee_rules" in {t["function"]["name"] for t in tools}) == enabled
        assert "$ref" not in json.dumps(tools)


@pytest.mark.parametrize("native", [False, True])
def test_rule_dispatch_and_evidence_submission(database, native):
    call = dict(name="analyze_fee_rules", args=arguments())
    responses = iter(
        [
            SimpleNamespace(
                content="" if native else json.dumps({call["name"]: call["args"]}),
                tool_calls=[call] if native else [],
                usage_metadata={"total_tokens": 1},
            ),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "submit_answers": {
                            "fields": [dict(name="answer", step=1, path=["average_fee"])]
                        }
                    }
                ),
                tool_calls=[],
                usage_metadata={"total_tokens": 1},
            ),
        ]
    )

    class Model:
        def invoke(self, messages):
            return next(responses)

    task = Task(
        "dabstep-dev",
        "synthetic",
        "Average fee for credit Alpha, value 10 EUR",
        "@answer[float] rounded to 6 decimals",
        "hard",
        "payments.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-rules").run(
        task, database, documents=DOCUMENT
    )
    assert result["failure"] is None
    assert float(result["answers"]["answer"]) == pytest.approx(0.064)
    receipt = result["trace"][0]["rule_evidence"]
    assert receipt["rule_citations"][0]["record"]["rate"] == 14
    assert receipt["source"].startswith("fees@sha256:")


def test_day_scope_uses_full_month_and_delta_is_exact(database):
    request = arguments("applicable", merchant="Shop", year=2023, day_of_year=10)
    result = execute(database, request)
    assert result["items"] == [1]
    assert result["context"]["volume"] == "400.0"
    assert result["context"]["fraud_ratio"] == "0.25"
    assert result["context"]["scoped_transactions"] == 1
    change = execute(
        database,
        arguments(
            "counterfactual", merchant="Shop", year=2023, month=1, rule_id="1", alternate_rate="1"
        ),
    )
    assert change["transaction_count"] == 2
    assert change["delta"] == pytest.approx(-0.52)


def test_invalid_source_requests_and_metadata_bindings_are_rejected(database):
    for request in [
        arguments(semantics_excerpt="invented"),
        arguments(rules_table="secret"),
        arguments("applicable", merchant="Shop", year=2023),
        arguments(unsupported_condition=True),
    ]:
        with pytest.raises(ValueError):
            execute(database, request)
    evidence = execute(database, arguments())
    with pytest.raises(SemanticError, match="computed_output"):
        assemble_answers(
            {"answer": (1, ["rule_citations", 0, "record", "rate"])},
            {1: evidence},
            AnswerSpec(columns=["answer"]),
        )


def test_complete_rule_collection_can_be_submitted(database):
    actions = iter(
        [
            {"analyze_fee_rules": arguments("select", account_type="R", aci="B")},
            {
                "submit_answers": {
                    "fields": [
                        dict(
                            name="answer",
                            step=1,
                            path=["items"],
                            rule=dict(
                                kind="list",
                                source_excerpt=(
                                    "Answer must be a list of values in comma separated list, "
                                    "eg: A, B, C."
                                ),
                                collection_separator=", ",
                            ),
                        )
                    ]
                }
            },
        ]
    )

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 1}
            )

    task = Task(
        "dabstep-dev",
        "synthetic-collection",
        "Applicable fee IDs for R and B",
        "Answer must be a list of values in comma separated list, eg: A, B, C.",
        "hard",
        "payments.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-rules").run(
        task, database, documents=DOCUMENT
    )
    assert result["failure"] is None
    assert str(result["answers"]["answer"]) == "1"


def test_bad_excerpt_has_safe_feedback_and_can_be_repaired(database):
    actions = iter(
        [
            {"analyze_fee_rules": arguments(semantics_excerpt="invented secret-like source")},
            {"analyze_fee_rules": arguments()},
            {"submit_answers": {"fields": [dict(name="answer", step=2, path=["average_fee"])]}},
        ]
    )
    observed = []

    class Model:
        def invoke(self, messages):
            observed.append(str(messages))
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 1}
            )

    task = Task(
        "dabstep-dev",
        "synthetic-repair",
        "Average fee",
        "@answer[float] rounded to 6 decimals",
        "hard",
        "payments.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-rules").run(
        task, database, documents=DOCUMENT
    )
    assert result["failure"] is None
    assert result["trace"][0]["code"] == "rule_semantics_excerpt_missing"
    assert result["trace"][0]["repair_type"] == "correct_rule_context"
    assert "invented secret-like source" not in json.dumps(result["trace"][0])
    assert "rule_repair_hint" in observed[1]
    assert float(result["answers"]["answer"]) == pytest.approx(0.064)


@pytest.mark.parametrize(
    "proposal,code",
    [
        (arguments("applicable", year=2023, month=1), "rule_context_required"),
        (arguments("applicable", merchant="Shop", year=2023), "rule_time_scope_required"),
        (
            arguments("applicable", merchant="Shop", year=2023, month=1, day_of_year=1),
            "rule_time_scope_required",
        ),
        (arguments("select", merchant="Shop"), "rule_context_operation_required"),
        (
            arguments("counterfactual", merchant="Shop", year=2023, month=1),
            "rule_rate_change_required",
        ),
        (arguments("select", rule_id="1"), "rule_counterfactual_operation_required"),
    ],
)
def test_rule_contract_errors_have_value_free_repair_hints(proposal, code):
    from generaldataagent.evaluation.stage_nine import rule_schema_repair_hints
    from generaldataagent.orchestration.analysis_plan import ActionSchemaError, validate_action

    with pytest.raises(ActionSchemaError) as failure:
        validate_action({"rules": proposal})
    assert failure.value.paths == [{"path": ["rules"], "type": code}]
    hints = rule_schema_repair_hints(failure.value.paths)
    assert len(hints) == 1
    assert "Shop" not in hints[0]
    assert rule_schema_repair_hints([{"path": ["analysis"], "type": code}]) == []


def test_missing_rate_parameters_can_be_repaired_before_execution(database):
    actions = iter(
        [
            {"analyze_fee_rules": arguments("counterfactual", merchant="Shop", year=2023, month=1)},
            {
                "analyze_fee_rules": arguments(
                    "counterfactual",
                    merchant="Shop",
                    year=2023,
                    month=1,
                    rule_id="1",
                    alternate_rate="1",
                )
            },
            {"submit_answers": {"fields": [dict(name="answer", step=2, path=["delta"])]}},
        ]
    )
    observed = []

    class Model:
        def invoke(self, messages):
            observed.append(str(messages))
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 1}
            )

    task = Task(
        "dabstep-dev",
        "synthetic-rate-repair",
        "Compute the fee rate delta",
        "@answer[float] rounded to 6 decimals",
        "hard",
        "payments.csv",
    )
    result = BoundedSQLAgent(Model(), tool_profile="sql-rules").run(
        task, database, documents=DOCUMENT
    )
    assert result["failure"] is None
    assert result["trace"][0]["schema_errors"][0]["type"] == "rule_rate_change_required"
    assert "Supply both rule_id and alternate_rate" in observed[1]
    assert float(result["answers"]["answer"]) == pytest.approx(-0.52)


def test_exact_percent_boundaries_and_nonfinite_observations():
    assert parse_band(">=8.3%").contains(Decimal("0.083"))
    assert not parse_band(">8.3%").contains(Decimal("0.083"))
    with pytest.raises(ValueError, match="finite"):
        parse_band("1-2").contains(float("nan"))
