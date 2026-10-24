"""Raw decimal amounts must retain their side of merchant rule thresholds."""

import json
import sqlite3
from fractions import Fraction

import pytest

from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.evaluation.stage_nine import analyze_fee_rules
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
EXCERPT = "A null predicate accepts all values."
DOCUMENTS = EXCERPT + "\nfee = fixed_amount + rate * transaction_value / 10000"


@pytest.mark.parametrize("execution", ["workflow", "evaluation_tool"])
@pytest.mark.parametrize("field", ["monthly_volume", "monthly_fraud_level"])
@pytest.mark.parametrize("side", ["above", "below"])
def test_exact_month_thresholds_survive_context_matching(tmp_path, execution, field, side):
    if field == "monthly_volume":
        amounts = [("10.0000000000000001" if side == "above" else "9.9999999999999999", False)]
        threshold, boundary = "10", Fraction(10)
    else:
        amounts = (
            [("1.0000000000000001", True), ("8.9999999999999999", False)]
            if side == "above"
            else [("0.9999999999999999", True), ("9.0000000000000001", False)]
        )
        threshold, boundary = "10%", Fraction(1, 10)
    # This oracle uses original records, never an engine/parser value.
    total = sum((Fraction(value) for value, _ in amounts), Fraction(0))
    observed = (
        total
        if field == "monthly_volume"
        else (sum((Fraction(value) for value, fraud in amounts if fraud), Fraction(0)) / total)
    )
    expected = ["above"] if observed > boundary else ["at_or_below"]
    annex = [
        dict(ID=key, card_scheme="Alpha", fixed_amount="0.05", rate="14", **{field: band})
        for key, band in (
            [("above", ">" + threshold), ("at_or_below", "<=" + threshold)]
            if side == "above"
            else [("above", ">=" + threshold), ("at_or_below", "<" + threshold)]
        )
    ]
    path = tmp_path / "payments.sqlite"
    with sqlite3.connect(path) as db:
        for table, records in [
            ("fees", annex),
            (
                "merchant_data",
                [dict(merchant="Shop", account_type="R", merchant_category_code=5411)],
            ),
        ]:
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            db.executemany(
                f"INSERT INTO {table} VALUES (?)", [(json.dumps(row),) for row in records]
            )
        # TEXT preserves the source amounts; REAL would already lose this precision.
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
            eur_amount TEXT, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.executemany(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("Shop", 2023, 1, value, str(fraud), "Alpha", "B", "True", "NL", "NL")
                for value, fraud in amounts
            ],
        )
    request = RuleAnalysisRequest(
        operation="applicable", semantics_excerpt=EXCERPT, merchant="Shop", year=2023, month=1
    )
    if execution == "evaluation_tool":
        result = analyze_fee_rules(
            path,
            request.model_dump(mode="json"),
            allowed_relations={"fees", "merchant_data", "payments"},
            documents=DOCUMENTS,
        )
    else:
        adapter = AnnexWorkflowServices(
            annex,
            documents=DOCUMENTS,
            payments=SQLitePaymentsSource(path, allowed_relations={"merchant_data", "payments"}),
        )
        with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
            runtime = RuleWorkflowRuntime(
                services=adapter.services(),
                checkpointer=saver,
                source_hashes=adapter.source_hashes,
                workflow_version="decimal-threshold-test-v1",
            )
            _, state = runtime.run(request, ACTOR)
        assert state["status"] == "completed", state.get("verification")
        assert state["verification"]["passed"] is True
        result = state["delivery"]
    assert result["items"] == expected
