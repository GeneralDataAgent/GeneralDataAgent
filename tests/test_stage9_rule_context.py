import json
import sqlite3

import pytest

from generaldataagent.business.rules.payments import (
    PaymentsSourceError,
    SQLitePaymentsSource,
    moment_of,
)
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
DOCUMENTS = (
    "A null predicate accepts all values.\nfee = fixed_amount + rate * transaction_value / 10000"
)
EXCERPT = "A null predicate accepts all values."
ANNEX = [
    dict(
        ID="A",
        card_scheme="Alpha",
        account_type=None,
        aci=None,
        merchant_category_code=None,
        intracountry=True,
        monthly_volume=">300",
        monthly_fraud_level=">10%",
        fixed_amount="0.05",
        rate="14",
    ),
    dict(
        ID="B",
        card_scheme="Alpha",
        account_type=None,
        aci=None,
        merchant_category_code=None,
        monthly_volume=">5000",
        fixed_amount="0.10",
        rate="20",
    ),
]


def database(tmp_path, *, rows=None):
    path = tmp_path / "payments.sqlite"
    merchant = dict(
        merchant="Shop", account_type="R", merchant_category_code=5411, capture_delay="1"
    )
    payments = rows or [
        ("Shop", 2023, 1, 100, "True", "Alpha", "B", "True", "NL", "NL"),
        ("Shop", 2023, 10, 300, "False", "Alpha", "B", "True", "NL", "BE"),
        ("Shop", 2023, 32, 900, "False", "Alpha", "B", "True", "NL", "NL"),
    ]
    with sqlite3.connect(path) as db:
        for table, values in [("fees", ANNEX), ("merchant_data", [merchant])]:
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            db.executemany(f"INSERT INTO {table} VALUES (?)", [(json.dumps(v),) for v in values])
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.executemany(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            payments,
        )
    return path


def source(path):
    return SQLitePaymentsSource(path, allowed_relations={"merchant_data", "payments"})


def run(tmp_path, request, *, payments=True, tamper=None):
    adapter = AnnexWorkflowServices(
        ANNEX,
        documents=DOCUMENTS,
        payments=source(tmp_path / "payments.sqlite") if payments else None,
    )
    ports = adapter.services()
    if tamper is not None:
        original = ports.compute
        ports = type(ports)(
            monthly_context=ports.monthly_context,
            match_rules=ports.match_rules,
            compute=lambda state: tamper(original(state)),
            verify=ports.verify,
            deliver=ports.deliver,
        )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = RuleWorkflowRuntime(
            services=ports,
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="annex-v2",
        )
        return flow.run(request, ACTOR)


def test_applicable_uses_month_totals_and_real_transactions(tmp_path):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="applicable",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        month=1,
    )
    _, result = run(tmp_path, request)
    assert result["status"] == "completed", result.get("verification")
    delivery = result["delivery"]
    assert delivery["items"] == ["A"]
    # Rule B exceeds the observed 400.00 monthly volume and is excluded.
    assert delivery["context"]["volume"] == "400"
    assert delivery["context"]["fraud_ratio"] == "0.25"
    assert delivery["context"]["transaction_count"] == 2


def test_day_scope_keeps_full_month_thresholds(tmp_path):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="applicable",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        day_of_year=1,
    )
    _, result = run(tmp_path, request)
    assert result["status"] == "completed", result.get("verification")
    assert result["delivery"]["items"] == ["A"]
    # Thresholds still come from the whole natural month, not the scoped day.
    assert result["delivery"]["context"]["volume"] == "400"
    assert result["delivery"]["context"]["scoped_transactions"] == 1


def test_counterfactual_delta_is_recomputed_by_the_verifier(tmp_path):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="counterfactual",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        day_of_year=1,
        rule_id="A",
        alternate_rate="20",
    )
    _, result = run(tmp_path, request)
    assert result["status"] == "completed", result.get("verification")
    delivery = result["delivery"]
    assert delivery["transaction_count"] == 1
    assert delivery["baseline_fee"] == "0.19"
    assert delivery["alternate_fee"] == "0.25"
    assert delivery["delta"] == "0.06"


def test_context_operation_without_payments_source_is_refused(tmp_path):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="applicable",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        month=1,
    )
    with pytest.raises(ValueError, match="requires_payments_source"):
        run(tmp_path, request, payments=False)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda result: result | {"items": ["A", "B"]},
        lambda result: result | {"total_count": 9},
    ],
)
def test_tampered_applicable_output_fails_verification(tmp_path, tamper):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="applicable",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        month=1,
    )
    _, result = run(tmp_path, request, tamper=tamper)
    assert result["status"] == "failed"
    assert "delivery" not in result


@pytest.mark.parametrize(
    "tamper",
    [
        lambda result: result | {"delta": "9.99"},
        lambda result: result | {"transaction_count": 3},
    ],
)
def test_tampered_counterfactual_output_fails_verification(tmp_path, tamper):
    database(tmp_path)
    request = RuleAnalysisRequest(
        operation="counterfactual",
        semantics_excerpt=EXCERPT,
        merchant="Shop",
        year=2023,
        day_of_year=1,
        rule_id="A",
        alternate_rate="20",
    )
    _, result = run(tmp_path, request, tamper=tamper)
    assert result["status"] == "failed"
    assert "delivery" not in result


def test_unknown_merchant_and_empty_month_are_stable_failures(tmp_path):
    database(tmp_path)
    scoped = source(tmp_path / "payments.sqlite")
    with pytest.raises(PaymentsSourceError) as unknown:
        scoped.merchant_year("Missing", 2023)
    assert unknown.value.code == "payments_merchant_not_unique"
    with pytest.raises(PaymentsSourceError) as empty:
        from generaldataagent.business.rules.payments import scoped_month

        scoped_month(scoped.merchant_year("Shop", 2023), 7)
    assert empty.value.code == "payments_month_empty"


def test_relation_allowlist_and_calendar_validation(tmp_path):
    database(tmp_path)
    narrow = SQLitePaymentsSource(
        tmp_path / "payments.sqlite", allowed_relations={"merchant_data", "payments"}
    )
    with pytest.raises(PaymentsSourceError) as denied:
        narrow._rows("fees")
    assert denied.value.code == "payments_relation_denied"
    with pytest.raises(ValueError):
        SQLitePaymentsSource(tmp_path / "payments.sqlite", allowed_relations={"payments"})
    with pytest.raises(PaymentsSourceError):
        moment_of({"year": 2023, "day_of_year": 0})
    with pytest.raises(PaymentsSourceError):
        moment_of({"year": 2023, "day_of_year": 366})


def test_payments_snapshot_changes_the_workflow_binding(tmp_path):
    path = database(tmp_path)
    before = source(path).snapshot_hash()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2023, 2, 50, "False", "Alpha", "B", "True", "NL", "NL"),
        )
    assert source(path).snapshot_hash() != before
