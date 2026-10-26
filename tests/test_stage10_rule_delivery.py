import json
from dataclasses import replace
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.business.rules.delivery import RuleDeliveryError, RuleDeliveryRuntime
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ArtifactRef, DeliveryPackage, ExecutionReceipt
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
    receipt = ExecutionReceipt.model_validate(result["receipt"])
    assert receipt.action == "rules.deliver"
    assert receipt.result_refs == package.evidence
    assert len(receipt.result_refs) == 3
    assert receipt.task_id == str(task_id)
    assert (receipt.actor_id, receipt.tenant_id, receipt.project_id) == ("u", "t", "p")
    assert receipt.trace_id == f"rule-delivery-{task_id}"
    assert {source.source_id: source.content_hash for source in receipt.sources} == result[
        "source_hashes"
    ]
    assert receipt.method.method_id == "rules.delivery"
    assert receipt.method.method_version == "delivery-v1"
    assert receipt.completeness == "complete"
    assert receipt.verification.scope == "authoritative_rederivation"
    assert receipt.verification.passed is True
    assert receipt.verification.check_codes == tuple(result["verification"]["checks"])
    assert receipt.usage.telemetry_complete is False
    assert all(
        value is None
        for key, value in receipt.usage.model_dump().items()
        if key not in {"schema_version", "telemetry_complete"}
    )
    assert "receipt" not in result["delivery"]
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
        ("applicable", {"items": []}, ("applicable_rules", "[]")),
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
        ("applicable", {}),
        ("applicable", {"items": None}),
        ("applicable", {"items": "[]"}),
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


@pytest.mark.parametrize("has_match", [True, False])
def test_context_delivery_states_the_month_threshold_limitation(tmp_path, has_match):
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
    annex = ANNEX if has_match else [row | {"monthly_volume": ">1000"} for row in ANNEX]
    feedback = FeedbackRegistry(tmp_path / "feedback.sqlite")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        adapter = AnnexWorkflowServices(
            annex,
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
            workflow=flow, artifacts=LocalArtifactStore(tmp_path / "artifacts"), feedback=feedback
        )
        scoped = request(operation="applicable", merchant="Shop", year=2023, day_of_year=1)
        thread, state = flow.run(scoped, ACTOR)
        assert state["status"] == "completed", state.get("verification")
        expected_items = ["A"] if has_match else []
        assert state["delivery"]["items"] == expected_items
        assert state["delivery"]["total_count"] == len(expected_items)
        result = service.deliver(thread, scoped, ACTOR, task_id=task_id)
    package = DeliveryPackage.model_validate(result["delivery"])
    assert any("whole natural month" in line for line in package.limitations)
    assert package.rows == (("applicable_rules", "A" if has_match else "[]"),)
    assert feedback.registered(task_id, ACTOR)
    data = next(ref for ref in package.evidence if ref.artifact_type == "rule_analysis_result")
    stored = json.loads(service.artifacts.read_bytes(data))
    assert stored["delivery"]["items"] == expected_items
    assert stored["verification"]["passed"] is True
    assert data.task_id == task_id
    assert package.claims[0].evidence_uris == (data.uri,)
    if not has_match:
        assert "applicable_rules = []" in package.summary


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
    from generaldataagent.policy import PolicyEngine, request_fingerprint

    requests = []

    class RecordingPolicy(PolicyEngine):
        def evaluate(self, proposal):
            requests.append(proposal)
            return super().evaluate(proposal)

    task_id = uuid4()
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, _ = runtime(tmp_path, saver=saver)
        service = RuleDeliveryRuntime(
            workflow=flow,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            audit=audit,
            policy=RecordingPolicy(),
        )
        thread, _ = flow.run(request(), ACTOR)
        service.deliver(thread, request(), ACTOR, task_id=task_id)
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(rows) == 4
    assert [proposal.tool_name for proposal in requests] == ["rules.deliver"] + [
        "artifact.write"
    ] * 3
    assert [proposal.resource for proposal in requests[1:]] == [
        "rule_analysis_result",
        "chart_spec",
        "rule_claims",
    ]
    assert [row["payload"]["request_fingerprint"] for row in rows] == [
        request_fingerprint(proposal) for proposal in requests
    ]
    assert all(proposal.agent_role == "data_analyst" for proposal in requests)
    assert all(proposal.data_domains == ACTOR.data_domains for proposal in requests)
    assert len({row["trace_id"] for row in rows}) == 1
    assert len({row["payload"]["request_fingerprint"] for row in rows}) == 4
    for row in rows:
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


@pytest.mark.parametrize("mode", ["missing", "role_denied"])
def test_artifact_policy_denial_prevents_writes_and_feedback(tmp_path, mode):
    from generaldataagent.policy import PolicyEngine
    from generaldataagent.policy.engine import DEFAULT_RULES

    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, before = flow.run(request(), ACTOR)
        rules = tuple(rule for rule in DEFAULT_RULES if rule.tool_name == "rules.deliver")
        if mode == "role_denied":
            artifact_rule = next(
                rule for rule in DEFAULT_RULES if rule.tool_name == "artifact.write"
            )
            rules += (replace(artifact_rule, allowed_roles=frozenset({"reporter"})),)
        service.policy = PolicyEngine(rules=rules)
        service.audit = JsonlAuditSink(tmp_path / "audit.jsonl")
        with pytest.raises(PolicyDeniedError):
            service.deliver(thread, request(), ACTOR, task_id=task_id)
        assert not service.feedback.registered(task_id, ACTOR)
        assert flow.state_of(thread, request(), ACTOR) == before
    assert not list((tmp_path / "artifacts").rglob("*.json"))
    rows = [json.loads(line) for line in service.audit.path.read_text().splitlines()]
    assert [row["payload"]["decision"] for row in rows] == ["allow", "deny"]


@pytest.mark.parametrize("fail_at", [2, 3, 4])
def test_artifact_audit_outage_stops_writes_before_feedback(tmp_path, fail_at):
    class Audit:
        def __init__(self):
            self.calls = 0

        def emit(self, event):
            self.calls += 1
            if self.calls == fail_at:
                raise OSError("synthetic artifact audit outage")

    task_id = uuid4()
    audit = Audit()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        service.audit = audit
        thread, before = flow.run(request(), ACTOR)
        with pytest.raises(OSError, match="synthetic artifact audit outage"):
            service.deliver(thread, request(), ACTOR, task_id=task_id)
        assert not service.feedback.registered(task_id, ACTOR)
        assert flow.state_of(thread, request(), ACTOR) == before
    assert audit.calls == fail_at
    assert len(list((tmp_path / "artifacts").rglob("*.json"))) == fail_at - 2


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


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "outside"},
        {"project_id": "outside"},
        {"data_domains": ("sales",)},
        {"data_domains": ()},
    ],
)
def test_governed_store_scope_mismatch_prevents_delivery_writes(tmp_path, changes):
    from generaldataagent.artifacts.governed import GovernedArtifactStore

    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, before = flow.run(request(), ACTOR)
        service.artifacts = GovernedArtifactStore(
            tmp_path / "governed-artifacts",
            index_path=tmp_path / "artifact-index.sqlite",
            actor=ACTOR.model_copy(update=changes),
        )
        service.audit = JsonlAuditSink(tmp_path / "delivery-audit.jsonl")
        with pytest.raises(PermissionError, match="artifact_store_actor_scope_mismatch"):
            service.deliver(thread, request(), ACTOR, task_id=task_id)
        assert not list(service.artifacts.root.rglob("*.json"))
        assert not service.feedback.registered(task_id, ACTOR)
        assert not service.audit.path.exists()
        assert flow.state_of(thread, request(), ACTOR) == before
    import sqlite3

    with sqlite3.connect(service.artifacts.index_path) as db:
        assert db.execute("SELECT COUNT(*) FROM governed_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("domains", [("payments",), ("payments", "sales")])
def test_matching_governed_store_scope_preserves_verified_delivery(tmp_path, domains):
    from generaldataagent.artifacts.governed import GovernedArtifactStore

    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, before = flow.run(request(), ACTOR)
        service.artifacts = GovernedArtifactStore(
            tmp_path / "governed-artifacts",
            index_path=tmp_path / "artifact-index.sqlite",
            actor=ACTOR.model_copy(update={"data_domains": domains}),
        )
        result = service.deliver(thread, request(), ACTOR, task_id=task_id)
        package = DeliveryPackage.model_validate(result["delivery"])
        assert package.rows == (("average_fee", "0.092"),)
        assert all(ref.task_id == task_id for ref in package.evidence)
        for ref in package.evidence:
            assert service.artifacts.read_bytes(ref)
        assert service.feedback.registered(task_id, ACTOR)
        assert flow.state_of(thread, request(), ACTOR) == before


def test_a_short_id_list_still_prints_in_full(tmp_path):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        selected = request(operation="select", card_scheme="Alpha")
        thread, _ = flow.run(selected, ACTOR)
        result = service.deliver(thread, selected, ACTOR, task_id=task_id)
    claim = DeliveryPackage.model_validate(result["delivery"]).claims[0].text
    assert claim == "Rule analysis (select) produced selected_rules = A."


def test_delivery_receipt_uses_composed_identity_and_trace(tmp_path):
    from test_execution_composition import product_composition

    from generaldataagent.contracts import Budget

    task_id = uuid4()
    composition = product_composition(tmp_path, task_id=task_id, trace_id="operator-trace")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, _ = runtime(tmp_path, saver=saver)
        service = RuleDeliveryRuntime(
            workflow=flow,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        composition.budgets.register(task_id, ACTOR, Budget())
        thread, _ = flow.run(request(), ACTOR)
        result = service.deliver(thread, request(), ACTOR, task_id=task_id, trace_id="caller-trace")
    receipt = ExecutionReceipt.model_validate(result["receipt"])
    assert receipt.task_id == str(task_id)
    assert receipt.actor_id == composition.actor.user_id
    assert receipt.trace_id == "operator-trace"
    assert receipt.result_refs == DeliveryPackage.model_validate(result["delivery"]).evidence


@pytest.mark.parametrize("handler_scope", ["authoritative_rederivation", "untrusted-strength"])
def test_legacy_checkpoint_delivery_receipt_remains_a_self_check(
    tmp_path, monkeypatch, handler_scope
):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, state = flow.run(request(), ACTOR)
        legacy = {key: value for key, value in state.items() if key != "verification_scope"}
        legacy["verification"] = {**legacy["verification"], "scope": handler_scope}
        monkeypatch.setattr(flow, "state_of", lambda *_: legacy)
        result = service.deliver(thread, request(), ACTOR, task_id=uuid4())
    assert result["receipt"]["verification"]["scope"] == "producer_self_check"


@pytest.mark.parametrize("artifact_number", [1, 2, 3])
@pytest.mark.parametrize("failure", ["substitution", "cross_task", "corruption", "read_failure"])
def test_delivery_refuses_untrustworthy_persisted_outputs(
    tmp_path, monkeypatch, artifact_number, failure
):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, before = flow.run(request(), ACTOR)
        original = service.artifacts.put_json
        original_read = service.artifacts.read_bytes
        calls = 0
        rejected_uri = None

        def persist(**arguments):
            nonlocal calls, rejected_uri
            calls += 1
            if calls == artifact_number:
                if failure == "substitution":
                    # The reference hash honestly describes the wrong bytes.
                    arguments["value"] = {"wrong": "self-consistent content"}
                elif failure == "cross_task":
                    arguments["task_id"] = uuid4()
            ref = original(**arguments)
            if calls == artifact_number:
                rejected_uri = ref.uri
                if failure == "corruption":
                    path = service.artifacts.root / ref.uri.removeprefix("artifact://local/")
                    path.write_bytes(b"corrupt")
            return ref

        def read(ref):
            if ref.uri == rejected_uri and failure == "read_failure":
                raise OSError("synthetic read failure")
            return original_read(ref)

        monkeypatch.setattr(service.artifacts, "put_json", persist)
        monkeypatch.setattr(service.artifacts, "read_bytes", read)
        expected_error = OSError if failure == "read_failure" else ValueError
        with pytest.raises(expected_error):
            service.deliver(thread, request(), ACTOR, task_id=task_id)
        assert not service.feedback.registered(task_id, ACTOR)
        assert calls == artifact_number
        assert flow.state_of(thread, request(), ACTOR) == before


def test_delivery_rechecks_all_outputs_before_registering_feedback(tmp_path, monkeypatch):
    task_id = uuid4()
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, _ = flow.run(request(), ACTOR)
        original = service.artifacts.read_bytes
        reads = 0

        def read(ref):
            nonlocal reads
            reads += 1
            if reads == 4:
                return b"changed after the initial read"
            return original(ref)

        monkeypatch.setattr(service.artifacts, "read_bytes", read)
        with pytest.raises(RuleDeliveryError, match="rule_delivery_artifact_bytes_mismatch"):
            service.deliver(thread, request(), ACTOR, task_id=task_id)
        assert not service.feedback.registered(task_id, ACTOR)


@pytest.mark.parametrize("checks", [[""], [12], "unstructured"])
def test_malformed_checkpoint_checks_fail_before_delivery_writes(tmp_path, monkeypatch, checks):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow, service = runtime(tmp_path, saver=saver)
        thread, state = flow.run(request(), ACTOR)
        state["verification"] = {**state["verification"], "checks": checks}
        monkeypatch.setattr(flow, "state_of", lambda *_: state)
        with pytest.raises(ValueError, match="rule_receipt_checks_invalid"):
            service.deliver(thread, request(), ACTOR, task_id=uuid4())
        assert not list(service.artifacts.root.rglob("*.json"))
