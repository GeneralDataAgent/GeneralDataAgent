import json
import sqlite3
from decimal import Decimal
from uuid import uuid4

import pytest

from generaldataagent.business.rules.engine import monthly_totals
from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.evaluation.stage_nine import analyze_fee_rules
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

EXCERPT = "A null predicate accepts all values."
DOCUMENTS = EXCERPT + "\nfee = fixed_amount + rate * transaction_value / 10000"
ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))


@pytest.mark.parametrize("indicator", [None, "", " \t ", "missing"])
def test_monthly_totals_requires_an_explicit_fraud_indicator(indicator):
    row = {"eur_amount": "100"}
    if indicator != "missing":
        row["has_fraudulent_dispute"] = indicator
    with pytest.raises(ValueError, match="fraud indicator required"):
        monthly_totals([row])


@pytest.mark.parametrize(
    "indicator, fraud",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("True", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("no", False),
        (" TRUE ", True),
    ],
)
def test_explicit_fraud_encodings_preserve_amounts_and_counts(indicator, fraud):
    assert monthly_totals([{"eur_amount": "12.50", "has_fraudulent_dispute": indicator}]) == (
        Decimal("12.50"),
        Decimal("12.50") if fraud else Decimal(0),
        1,
        int(fraud),
    )


@pytest.mark.parametrize("execution", ["workflow", "evaluation_tool"])
@pytest.mark.parametrize("operation", ["applicable", "counterfactual"])
@pytest.mark.parametrize("indicator", [None, "", " \t ", "missing"])
def test_unknown_fraud_elsewhere_in_month_blocks_day_scoped_answer(
    tmp_path, execution, operation, indicator
):
    annex = [
        dict(
            ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14", monthly_fraud_level="<10%"
        )
    ]
    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
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
        # The requested day is valid; the unknown indicator is on another day
        # that still contributes to the full month's fraud threshold.
        fraud_column = "" if indicator == "missing" else ", has_fraudulent_dispute TEXT"
        db.execute(f"""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
            eur_amount TEXT, card_scheme TEXT, aci TEXT, is_credit TEXT,
            issuing_country TEXT, acquirer_country TEXT{fraud_column})""")
        rows = [
            ("Shop", 2024, day, "100", "Alpha", "B", "True", "NL", "NL")
            + (() if indicator == "missing" else ("False" if day == 1 else indicator,))
            for day in (1, 2)
        ]
        placeholders = ",".join("?" for _ in rows[0])
        db.executemany(f"INSERT INTO payments VALUES ({placeholders})", rows)
    request = RuleAnalysisRequest(
        operation=operation,
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2024,
        day_of_year=1,
        **({"rule_id": "A", "alternate_rate": "20"} if operation == "counterfactual" else {}),
    )
    if execution == "evaluation_tool":
        with pytest.raises(ValueError, match="fraud indicator required"):
            analyze_fee_rules(
                database,
                request.model_dump(mode="json"),
                allowed_relations={"fees", "merchant_data", "payments"},
                documents=DOCUMENTS,
            )
    else:
        adapter = AnnexWorkflowServices(
            annex,
            documents=DOCUMENTS,
            payments=SQLitePaymentsSource(
                database, allowed_relations={"merchant_data", "payments"}
            ),
        )
        with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
            flow = RuleWorkflowRuntime(
                services=adapter.services(),
                checkpointer=saver,
                source_hashes=adapter.source_hashes,
                workflow_version="fraud-required-v1",
            )
            thread = str(uuid4())
            with pytest.raises(ValueError, match="fraud indicator required"):
                flow.run(request, ACTOR, thread_id=thread)
            state = flow.state_of(thread, request, ACTOR)
            assert state["status"] == "created"
            assert "computation" not in state
            assert "delivery" not in state
