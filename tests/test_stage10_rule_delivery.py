import json
from dataclasses import replace
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.business.rules.delivery import RuleDeliveryError, RuleDeliveryRuntime
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ArtifactRef, DeliveryPackage
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.tools.gateway import PolicyDeniedError

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
DOCUMENTS = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"
ANNEX = [
    dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14"),
    dict(ID="B", card_scheme="Beta", fixed_amount="0.10", rate="20"),
]


def runtime(tmp_path, *, saver, tamper=None, annex=None):
    adapter = AnnexWorkflowServices(annex or ANNEX, documents=DOCUMENTS)
    ports = adapter.services()
    if tamper is not None:
        original = ports.compute
        ports = replace(ports, compute=lambda state: tamper(original(state)))
    flow = RuleWorkflowRuntime(
        services=ports,
        checkpointer=saver,
        source_hashes=adapter.source_hashes,
        workflow_version="delivery-v1",
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    feedback = FeedbackRegistry(tmp_path / "feedback.sqlite")
    return flow, RuleDeliveryRuntime(workflow=flow, artifacts=artifacts, feedback=feedback)


def request(**changes):
    values = dict(operation="average_fee", semantics_excerpt="null means all")
    return RuleAnalysisRequest(**(values | changes))


def test_verified_thread_is_packaged_with_cited_artifacts(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        result = service.deliver(thread, request(), ACTOR, task_id=task_id)
    package = DeliveryPackage.model_validate(result["delivery"])
    assert result["production_accepted"] is False
    assert package.metric == "rule:average_fee"
    assert package.columns == ("answer", "value")
    assert package.row_count == 1
    # The claim cites an artifact that belongs to this task.
    evidence_uris = {item.uri for item in package.evidence}
    assert set(package.claims[0].evidence_uris) <= evidence_uris
    assert all(item.task_id == task_id for item in package.evidence)
    assert package.chart.data_artifact_uri == package.table.data_artifact_uri
    assert any("not an independent semantic oracle" in line for line in package.limitations)
    # Every artifact is readable and hash-verified.
    for ref in package.evidence:
        ArtifactRef.model_validate(ref.model_dump(mode="json"))


def test_feedback_registration_uses_the_packaged_result_hash(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        result = service.deliver(thread, request(), ACTOR, task_id=task_id)
    expected = content_hash(result["delivery"])
    import sqlite3

    with sqlite3.connect(service.feedback.path) as db:
        row = db.execute("SELECT hash FROM feedback_tasks WHERE task=?", (str(task_id),)).fetchone()
    assert row[0] == expected


def test_failed_verification_cannot_be_packaged(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(
            tmp_path, saver=saver, tamper=lambda result: result | {"average_fee": "999"}
        )
        thread, state = flow.run(request(), ACTOR)
        assert state["status"] == "failed"
        with pytest.raises(RuleDeliveryError) as denied:
            service.deliver(thread, request(), ACTOR, task_id=task_id)
    assert denied.value.code == "rule_delivery_requires_completed_thread"
    assert not list((tmp_path / "artifacts").rglob("*.json"))


def test_unfinished_thread_cannot_be_packaged(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR, interrupt_after=("compute",))
        with pytest.raises(RuleDeliveryError) as denied:
            service.deliver(thread, request(), ACTOR, task_id=task_id)
    assert denied.value.code == "rule_delivery_requires_completed_thread"


@pytest.mark.parametrize(
    "operation,delivery,expected",
    [
        ("applicable", {"items": ["A", "B"]}, ("applicable_rules", "A, B")),
        ("counterfactual", {"delta": "0.06"}, ("delta", "0.06")),
        ("average_fee", {"average_fee": "0.064"}, ("average_fee", "0.064")),
        ("select", {"items": []}, ("selected_rules", "")),
    ],
)
def test_each_operation_maps_to_one_deterministic_row(operation, delivery, expected):
    from generaldataagent.business.rules.delivery import answer_rows

    assert answer_rows(delivery, operation) == [expected]


@pytest.mark.parametrize(
    "operation,delivery",
    [
        ("applicable", {"items": []}),
        ("counterfactual", {}),
        ("average_fee", {"average_fee": None}),
        ("select", {}),
    ],
)
def test_missing_answer_values_are_refused(operation, delivery):
    from generaldataagent.business.rules.delivery import answer_rows

    with pytest.raises(RuleDeliveryError) as denied:
        answer_rows(delivery, operation)
    assert denied.value.code == "rule_delivery_evidence_missing"


def test_unknown_operation_is_refused():
    from generaldataagent.business.rules.delivery import answer_rows

    with pytest.raises(RuleDeliveryError) as denied:
        answer_rows({"items": ["A"]}, "not_an_operation")
    assert denied.value.code == "rule_delivery_unknown_operation"


def test_delivery_requires_a_supported_artifact_store(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        adapter = AnnexWorkflowServices(ANNEX, documents=DOCUMENTS)
        flow = RuleWorkflowRuntime(
            services=adapter.services(),
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="delivery-v1",
        )
        with pytest.raises(ValueError, match="artifact_store"):
            RuleDeliveryRuntime(workflow=flow, artifacts=object())


def test_context_delivery_states_the_month_threshold_limitation(tmp_path):
    import sqlite3

    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute(
            "INSERT INTO merchant_data VALUES (?)",
            (json.dumps(dict(merchant="Shop", account_type="R", capture_delay="1")),),
        )
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2023, 1, 100, "False", "Alpha", "A", "True", "NL", "NL"),
        )
    from generaldataagent.business.rules.payments import SQLitePaymentsSource

    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        adapter = AnnexWorkflowServices(
            ANNEX,
            documents=DOCUMENTS,
            payments=SQLitePaymentsSource(
                database, allowed_relations={"merchant_data", "payments"}
            ),
        )
        flow = RuleWorkflowRuntime(
            services=adapter.services(),
            checkpointer=saver,
            source_hashes=adapter.source_hashes,
            workflow_version="delivery-v1",
        )
        service = RuleDeliveryRuntime(
            workflow=flow, artifacts=LocalArtifactStore(tmp_path / "artifacts")
        )
        scoped = request(operation="applicable", merchant="Shop", year=2023, day_of_year=1)
        thread, state = flow.run(scoped, ACTOR)
        assert state["status"] == "completed", state.get("verification")
        result = service.deliver(thread, scoped, ACTOR, task_id=task_id)
    package = DeliveryPackage.model_validate(result["delivery"])
    assert any("whole natural month" in line for line in package.limitations)
    assert package.rows[0][0] == "applicable_rules"


def test_delivery_is_idempotent_by_refusal_not_by_duplicate(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        first = service.deliver(thread, request(), ACTOR, task_id=task_id)
        with pytest.raises(RuleDeliveryError) as denied:
            service.deliver(thread, request(), ACTOR, task_id=task_id)
    assert denied.value.code == "rule_delivery_already_packaged"
    # Both annex rules apply to an unrestricted request:
    # (0.05 + 14*10/10000) and (0.10 + 20*10/10000) average to 0.092.
    assert first["delivery"]["table"]["rows"] == [["average_fee", "0.092"]]
    assert first["delivery"]["row_count"] == 1


def test_delivery_does_not_run_a_new_task(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        service.deliver(thread, request(), ACTOR, task_id=task_id)
        # The workflow thread is unchanged and already finished; delivery only
        # packages it and adds no node execution.
        state = flow.resume(thread, request(), ACTOR)
    assert state["status"] == "completed"
    assert state["verification"]["passed"] is True


def test_delivery_is_authorized_and_audited_by_the_gateway(tmp_path):
    task_id = uuid4()
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, _ = runtime(tmp_path, saver=saver)
        service = RuleDeliveryRuntime(
            workflow=flow,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            audit=audit,
        )
        thread, _ = flow.run(request(), ACTOR)
        service.deliver(thread, request(), ACTOR, task_id=task_id)
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == str(task_id)
    assert row["event_type"] == "policy.decision"
    assert row["payload"]["decision"] == "allow"
    assert row["payload"]["risk_level"] == "r1"
    assert row["payload"]["request_fingerprint"].startswith("sha256:")


@pytest.mark.parametrize("role", ["supervisor", "data_engineer"])
def test_policy_denial_creates_no_package_and_no_feedback(tmp_path, role):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        with pytest.raises(PolicyDeniedError):
            service.deliver(thread, request(), ACTOR, task_id=task_id, agent_role=role)
        assert not service.feedback.registered(task_id, ACTOR)
    assert not list((tmp_path / "artifacts").rglob("*.json"))


def test_unlisted_tool_is_denied_by_a_stricter_policy(tmp_path):
    """Fail closed: the delivery path inherits whatever policy it is given."""
    from generaldataagent.policy import PolicyEngine

    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        service.policy = PolicyEngine(rules=())
        thread, _ = flow.run(request(), ACTOR)
        with pytest.raises(PolicyDeniedError) as denied:
            service.deliver(thread, request(), ACTOR, task_id=task_id)
    assert "unknown_tool" in str(denied.value)
    assert not list((tmp_path / "artifacts").rglob("*.json"))


def test_a_long_id_list_is_counted_not_truncated(tmp_path):
    """A 416-ID answer overflows the 2000-character claim contract.

    The claim must not be shortened — a truncated list reads as the complete
    answer — so it states the count and points at the cited artifact.
    """
    task_id = uuid4()
    annex = [
        dict(ID=str(index), card_scheme="Alpha", fixed_amount="0.05", rate="14")
        for index in range(1, 417)
    ]
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver, annex=annex)
        selected = request(operation="select", card_scheme="Alpha")
        thread, state = flow.run(selected, ACTOR)
        assert len(state["delivery"]["items"]) == 416
        result = service.deliver(thread, selected, ACTOR, task_id=task_id)
    package = DeliveryPackage.model_validate(result["delivery"])
    claim = package.claims[0].text
    assert claim == (
        "Rule analysis (select) produced selected_rules with 416 values; "
        "the complete list is in the cited artifact."
    )
    # The full answer is still in the package rows, not only in the artifact.
    assert len(str(package.rows[0][1]).split(", ")) == 416


def test_a_short_id_list_still_prints_in_full(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        selected = request(operation="select", card_scheme="Alpha")
        thread, _ = flow.run(selected, ACTOR)
        result = service.deliver(thread, selected, ACTOR, task_id=task_id)
    claim = DeliveryPackage.model_validate(result["delivery"]).claims[0].text
    assert claim == "Rule analysis (select) produced selected_rules = A."
