import json
import sqlite3
from datetime import date
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.payments import (
    PaymentsSourceError,
    SQLitePaymentsSource,
    moment_of,
)
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ToolRequest
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime


@pytest.mark.parametrize("field", ["year", "day_of_year"])
@pytest.mark.parametrize("invalid", [True, False, 1.5, 2024.5, float("inf"), None, "1.5"])
def test_source_calendar_rejects_non_integer_values(field, invalid):
    row = {"year": 2024, "day_of_year": 1, field: invalid}
    with pytest.raises(PaymentsSourceError) as error:
        moment_of(row)
    assert error.value.code == "payments_invalid_row"


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"year": 2024, "day_of_year": 60}, date(2024, 2, 29)),
        ({"year": "2024", "day_of_year": "366"}, date(2024, 12, 31)),
        ({"year": 2023, "day_of_year": 365}, date(2023, 12, 31)),
    ],
)
def test_source_calendar_preserves_integer_and_digit_text_dates(row, expected):
    assert moment_of(row) == expected


@pytest.mark.parametrize("operation", ["applicable", "counterfactual"])
def test_fractional_source_day_cannot_produce_governed_completion_evidence(tmp_path, operation):
    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute(
            "INSERT INTO merchant_data VALUES (?)",
            (
                json.dumps(
                    {"merchant": "Shop", "account_type": "R", "merchant_category_code": 5411}
                ),
            ),
        )
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year REAL,
                      eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT,
                      aci TEXT, is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2024, 1.5, 100, "False", "Alpha", "B", "True", "NL", "NL"),
        )
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
    adapter = AnnexWorkflowServices(
        [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")],
        documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
        payments=SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"}),
    )
    proposal = ToolRequest(
        task_id=uuid4(),
        user_id=actor.user_id,
        tenant_id=actor.tenant_id,
        project_id=actor.project_id,
        agent_role="data_analyst",
        tool_name="rules.execute",
        action="run",
        resource="rule-workflow",
        data_domains=actor.data_domains,
        arguments=dict(
            operation=operation,
            semantics_excerpt="null means all",
            merchant="Shop",
            year=2024,
            day_of_year=1,
            **({"rule_id": "A", "alternate_rate": "20"} if operation == "counterfactual" else {}),
        ),
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = RuleWorkflowRuntime(
            services=adapter.services(),
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="calendar-source-v1",
        )
        store = LocalArtifactStore(tmp_path / "artifacts")
        audit = JsonlAuditSink(tmp_path / "audit.jsonl")
        runner = GovernedRuleRunner(runtime=flow, actor=actor, artifacts=store, audit=audit)
        with pytest.raises(PaymentsSourceError) as error:
            runner.invoke(proposal, trace_id="invalid-source-calendar")
        assert error.value.code == "payments_invalid_row"
        state = flow.state_of(
            str(proposal.task_id), RuleAnalysisRequest.model_validate(proposal.arguments), actor
        )
        assert state["status"] == "created"
        assert "computation" not in state
        assert "delivery" not in state
        assert not list(store.root.iterdir())
        events = [json.loads(line) for line in audit.path.read_text().splitlines()]
        assert len(events) == 1
        assert events[0]["payload"]["decision"] == "allow"


@pytest.mark.parametrize(
    "row, expected",
    [
        # A numeric CSV column is imported as REAL, so SQLite returns floats.
        ({"year": 2023.0, "day_of_year": 10.0}, date(2023, 1, 10)),
        ({"year": 2024.0, "day_of_year": 60.0}, date(2024, 2, 29)),
    ],
)
def test_imported_real_columns_are_accepted_when_exactly_integral(row, expected):
    assert moment_of(row) == expected


@pytest.mark.parametrize("invalid", [2023.5, float("nan"), float("inf")])
def test_non_integral_real_columns_are_still_refused(invalid):
    with pytest.raises(PaymentsSourceError) as error:
        moment_of({"year": invalid, "day_of_year": 1.0})
    assert error.value.code == "payments_invalid_row"


def test_imported_snapshot_reaches_month_scope_for_a_day_of_year_request(tmp_path):
    """The regression: a REAL calendar column rejected every imported row."""
    from generaldataagent.business.rules.application import scope_rule_month
    from generaldataagent.business.rules.payments import SQLitePaymentsSource
    from generaldataagent.evaluation.stage_six import build_csv_database

    database = tmp_path / "payments.sqlite"
    csv_path = tmp_path / "payments.csv"
    csv_path.write_text(
        "merchant,year,day_of_year,eur_amount,has_fraudulent_dispute\n"
        "Shop,2023,10,100.0,False\n"
        "Shop,2023,20,50.0,False\n"
        "Shop,2023,300,25.0,False\n"
    )
    build_csv_database({"payments": csv_path}, database)
    source = SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"})
    rows = source._rows("payments")
    assert all(type(row["year"]) is float for row in rows)
    scope = scope_rule_month(rows, month=1, day_of_year=10)
    assert scope.transaction_count == 2
    assert len(scope.scoped_rows) == 1
