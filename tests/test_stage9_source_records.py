import json
import sqlite3
from uuid import uuid4

import pytest

from generaldataagent.business.rules.payments import PaymentsSourceError, SQLitePaymentsSource
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.evaluation.stage_nine import RuleToolError, analyze_fee_rules
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
DOCUMENTS = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"


def build(tmp_path, merchants, *, annex=("{",)):
    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.executemany("INSERT INTO fees VALUES (?)", [(row,) for row in annex])
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.executemany("INSERT INTO merchant_data VALUES (?)", [(row,) for row in merchants])
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
            eur_amount TEXT, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2024, 1, "100", "False", "Alpha", "B", "True", "NL", "NL"),
        )
    return database


def direct(database, operation="select", **extra):
    return analyze_fee_rules(
        database,
        dict(
            operation=operation,
            semantics_excerpt="null means all",
            **extra,
            **(
                {"merchant": "Shop", "year": 2024, "month": 1}
                if operation in {"applicable", "counterfactual"}
                else {"card_scheme": "Alpha"}
            ),
        ),
        allowed_relations={"fees", "merchant_data", "payments"},
        documents=DOCUMENTS,
    )


@pytest.mark.parametrize(
    ("payload", "leaked"),
    [("{", '{"'), ('["not", "an", "object"]', '"an"')],
)
def test_direct_rule_evaluation_refuses_non_object_source_records(tmp_path, payload, leaked):
    database = build(
        tmp_path,
        [payload],
        annex=(json.dumps(dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")),),
    )
    with pytest.raises(RuleToolError) as error:
        direct(database, "applicable")
    assert error.value.code == "rule_source_record_invalid"
    assert leaked not in error.value.hint


def test_direct_rule_evaluation_refuses_malformed_rule_payload(tmp_path):
    database = build(
        tmp_path,
        [json.dumps(dict(merchant="Shop", account_type="R", merchant_category_code=5411))],
    )
    with pytest.raises(RuleToolError) as error:
        direct(database)
    assert error.value.code == "rule_source_record_invalid"


def test_direct_rule_evaluation_requires_an_identifiable_merchant(tmp_path):
    database = build(
        tmp_path,
        [json.dumps({"account_type": "R"})],
        annex=(json.dumps(dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")),),
    )
    with pytest.raises(RuleToolError) as error:
        direct(database, "applicable")
    assert error.value.code == "rule_source_record_invalid"


@pytest.mark.parametrize(
    "record",
    [
        {"ID": "A", "card_scheme": "Alpha", "rate": "14"},
        {"card_scheme": "Alpha", "fixed_amount": "0.05", "rate": "14"},
        {"ID": "A", "card_scheme": "Alpha", "fixed_amount": "0.05", "rate": "14", "mystery": 1},
        {"ID": "A", "card_scheme": "Alpha", "fixed_amount": True, "rate": "14"},
    ],
)
def test_direct_rule_evaluation_refuses_rule_records_outside_the_contract(tmp_path, record):
    database = build(
        tmp_path,
        [json.dumps(dict(merchant="Shop", account_type="R", merchant_category_code=5411))],
        annex=(json.dumps(record),),
    )
    with pytest.raises(RuleToolError) as error:
        direct(database)
    assert error.value.code == "rule_source_record_invalid"
    assert error.value.hint == RuleToolError.HINTS["rule_source_record_invalid"]


def test_direct_rule_evaluation_refuses_duplicate_rule_ids(tmp_path):
    record = json.dumps(dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14"))
    database = build(
        tmp_path,
        [json.dumps(dict(merchant="Shop", account_type="R", merchant_category_code=5411))],
        annex=(record, record),
    )
    with pytest.raises(RuleToolError) as error:
        direct(database)
    assert error.value.code == "rule_source_record_invalid"


def test_source_refuses_non_object_merchant_records(tmp_path):
    path = tmp_path / "payments.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute("INSERT INTO merchant_data VALUES (?)", ('["not", "an", "object"]',))
        db.execute("CREATE TABLE payments (merchant TEXT, year INT)")
    source = SQLitePaymentsSource(path, allowed_relations={"merchant_data", "payments"})
    with pytest.raises(PaymentsSourceError) as error:
        source.merchant_year("Shop", 2024)
    assert error.value.code == "payments_invalid_row"


@pytest.mark.parametrize("payload", ["{", '["not", "an", "object"]'])
def test_rule_workflow_refuses_corrupt_merchant_records(tmp_path, payload):
    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.execute(
            "INSERT INTO fees VALUES (?)",
            (json.dumps(dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")),),
        )
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute("INSERT INTO merchant_data VALUES (?)", (payload,))
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
            eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2024, 1, 100, "False", "Alpha", "B", "True", "NL", "NL"),
        )
    adapter = AnnexWorkflowServices(
        [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")],
        documents=DOCUMENTS,
        payments=SQLitePaymentsSource(database, allowed_relations={"merchant_data", "payments"}),
    )
    request = RuleAnalysisRequest(
        operation="applicable",
        semantics_excerpt="null means all",
        merchant="Shop",
        year=2024,
        month=1,
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = RuleWorkflowRuntime(
            services=adapter.services(),
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="source-records-v1",
        )
        thread = str(uuid4())
        with pytest.raises(PaymentsSourceError) as error:
            flow.run(request, ACTOR, thread_id=thread)
        assert error.value.code == "payments_invalid_row"
        # The source snapshot is hashed before the thread is registered, so a
        # corrupt merchant record fails the run without creating a checkpoint.
        with pytest.raises(PermissionError, match="checkpoint_access_denied"):
            flow.state_of(thread, request, ACTOR)
