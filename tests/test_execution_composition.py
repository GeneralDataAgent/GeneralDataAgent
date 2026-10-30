"""Explicit research/product execution compositions are wiring, not authorization."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.business.rules.delivery import RuleDeliveryRuntime
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ArtifactRef, Budget, ToolRequest
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    AuditUnavailableError,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.composition import CompositionError, ExecutionComposition
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy.budgets import BudgetExceededError, TaskBudgetLedger

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
OTHER = ACTOR.model_copy(update={"tenant_id": "other"})
TASK = uuid4()
DOCUMENTS = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"
ANNEX = [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")]


class ReceiptTransport:
    """Minimal independent audit service: signs each append, can be told to fail."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.now = datetime.now(UTC)
        self.sequence = 0
        self.fail = False
        self.events: list[dict] = []

    def __call__(self, content: bytes) -> AuditReceipt:
        if self.fail:
            raise RuntimeError("audit service down")
        self.sequence += 1
        self.events.append(json.loads(content))
        receipt = AuditReceipt(
            event_id=json.loads(content)["event_id"],
            event_sha256=hashlib.sha256(content).hexdigest(),
            sequence=self.sequence,
            recorded_at=self.now,
            signer_id="audit",
            signature="",
        )
        signature = base64.b64encode(self.private.sign(receipt_statement(receipt))).decode()
        return receipt.model_copy(update={"signature": signature})

    def sink(self) -> VerifiedAuditSink:
        return VerifiedAuditSink(
            transport=self,
            trusted_keys={"audit": self.private.public_key()},
            pseudonym_key=b"k" * 32,
            clock=lambda: self.now,
        )


def governed_store(tmp_path, *, actor=ACTOR):
    return GovernedArtifactStore(
        tmp_path / "governed",
        index_path=tmp_path / "index.sqlite",
        actor=actor,
    )


def research_composition(tmp_path, *, budgets=None):
    return ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        budgets=budgets,
    )


def product_composition(tmp_path, *, artifacts=None, audit=None, budgets=None, **identity):
    transport = ReceiptTransport()
    return ExecutionComposition(
        mode="product",
        actor=ACTOR,
        artifacts=artifacts or governed_store(tmp_path),
        audit=audit or transport.sink(),
        budgets=budgets or TaskBudgetLedger(tmp_path / "budgets.sqlite"),
        **identity,
    )


def runtime(tmp_path, saver):
    adapter = AnnexWorkflowServices(ANNEX, documents=DOCUMENTS)
    return RuleWorkflowRuntime(
        services=adapter.services(),
        checkpointer=saver,
        source_hashes=adapter.source_hashes,
        workflow_version="composition-v1",
    )


def proposal(**changes):
    values = dict(
        task_id=uuid4(),
        user_id="u",
        tenant_id="t",
        project_id="p",
        agent_role="data_analyst",
        tool_name="rules.execute",
        action="run",
        resource="rule-workflow",
        data_domains=("payments",),
        arguments=dict(operation="average_fee", semantics_excerpt="null means all"),
    )
    return ToolRequest(**(values | changes))


def test_research_composition_admits_development_ports(tmp_path):
    composition = research_composition(tmp_path)
    assert composition.mode == "research"
    assert composition.production_ready is False


@pytest.mark.parametrize(
    "change,code",
    [
        ("mode", "composition_mode_unknown"),
        ("artifacts", "composition_artifact_store_required"),
        ("audit", "composition_audit_required"),
    ],
)
def test_composition_rejects_units_it_cannot_guarantee(tmp_path, change, code):
    values = dict(
        mode="research",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    values[change] = object() if change != "mode" else "pilot"
    with pytest.raises(CompositionError, match=code):
        ExecutionComposition(**values)


@pytest.mark.parametrize(
    "change", ["invalid_type", "user_id", "tenant_id", "project_id", "domains"]
)
def test_workload_composition_refusals_keep_their_declared_code(tmp_path, change):
    from generaldataagent.security.science_scope import ScienceWorkloadScope

    scope_values = dict(
        user_id=ACTOR.user_id,
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domains=frozenset(ACTOR.data_domains),
        task_id=uuid4(),
        snapshot_id=uuid4(),
    )
    if change in {"user_id", "tenant_id", "project_id"}:
        scope_values[change] = "other"
    elif change == "domains":
        scope_values["data_domains"] = frozenset()
    scope = object() if change == "invalid_type" else ScienceWorkloadScope(**scope_values)
    code = (
        "composition_workload_scope_invalid"
        if change == "invalid_type"
        else "composition_workload_scope_actor_mismatch"
    )
    with pytest.raises(CompositionError, match=code) as error:
        ExecutionComposition(
            mode="research",
            actor=ACTOR,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
            workload_scope=scope,
        )
    assert error.value.code == code


def test_product_composition_refuses_each_missing_control(tmp_path):
    values = dict(
        mode="product",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    with pytest.raises(CompositionError, match="composition_product_requires_governed_artifacts"):
        ExecutionComposition(**values)
    values["artifacts"] = governed_store(tmp_path)
    with pytest.raises(CompositionError, match="composition_product_requires_verified_audit"):
        ExecutionComposition(**values)
    values["audit"] = ReceiptTransport().sink()
    with pytest.raises(CompositionError, match="composition_product_requires_budgets"):
        ExecutionComposition(**values)
    values["budgets"] = TaskBudgetLedger(tmp_path / "budgets.sqlite")
    assert ExecutionComposition(**values).production_ready is True


def test_artifact_scope_mismatch_is_refused_at_composition_time(tmp_path):
    with pytest.raises(CompositionError, match="composition_artifact_scope_mismatch"):
        ExecutionComposition(
            mode="research",
            actor=ACTOR,
            artifacts=governed_store(tmp_path, actor=OTHER),
            audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        )


def test_product_runner_requires_ports_that_match_its_composition(tmp_path):
    composition = product_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(tmp_path, saver)
        # A development store cannot stand in for the governed one even when the
        # rest of the wiring matches: that is the guarantee, not the identity.
        with pytest.raises(ValueError, match="rule_runner_product_composition_requires"):
            GovernedRuleRunner(
                runtime=flow,
                actor=ACTOR,
                artifacts=LocalArtifactStore(tmp_path / "other"),
                audit=composition.audit,
                composition=composition,
            )
        # A different governed store with the same actor satisfies the guarantee
        # but not the composition's own port identity.
        with pytest.raises(ValueError, match="rule_runner_composition_ports_disagree"):
            GovernedRuleRunner(
                runtime=flow,
                actor=ACTOR,
                artifacts=governed_store(tmp_path / "second"),
                audit=composition.audit,
                composition=composition,
            )
        with pytest.raises(PermissionError, match="artifact_store_actor_scope_mismatch"):
            GovernedRuleRunner(
                runtime=flow,
                actor=ACTOR,
                artifacts=governed_store(tmp_path, actor=OTHER),
                audit=composition.audit,
            )


def test_product_composition_runs_only_under_a_registered_budget(tmp_path):
    composition = product_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runner = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        request = proposal()
        # No task budget registered yet: the production gateway fails closed.
        with pytest.raises(BudgetExceededError, match="task_budget_access_denied"):
            runner.invoke(request, trace_id="trace-unbudgeted")
        composition.budgets.register(request.task_id, ACTOR, Budget())
        result = runner.invoke(request, trace_id="trace-budgeted")
        assert result["status"] == "completed"
        assert result["production_accepted"] is False
        assert composition.artifacts.read_bytes(ArtifactRef.model_validate(result["evidence"]))


def test_product_composition_refuses_when_the_audit_service_is_down(tmp_path):
    composition = product_composition(tmp_path)
    transport = composition.audit.transport
    assert isinstance(transport, ReceiptTransport)
    transport.fail = True
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runner = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        request = proposal()
        composition.budgets.register(request.task_id, ACTOR, Budget())
        with pytest.raises(AuditUnavailableError):
            runner.invoke(request, trace_id="trace-outage")


def test_product_delivery_requires_verified_audit_and_matching_actor(tmp_path):
    composition = product_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(tmp_path, saver)
        request = RuleAnalysisRequest(operation="average_fee", semantics_excerpt="null means all")
        thread, _ = flow.run(request, ACTOR)
        with pytest.raises(ValueError, match="product_composition_requires_verified_audit"):
            RuleDeliveryRuntime(
                workflow=flow,
                artifacts=composition.artifacts,
                audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
                composition=composition,
            )
        service = RuleDeliveryRuntime(
            workflow=flow,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        with pytest.raises(PermissionError, match="rule_delivery_composition_actor_mismatch"):
            service.deliver(thread, request, OTHER, task_id=uuid4())
        task_id = uuid4()
        composition.budgets.register(task_id, ACTOR, Budget())
        result = service.deliver(thread, request, ACTOR, task_id=task_id)
        assert result["production_accepted"] is False
        assert result["delivery"]["rows"] == [["average_fee", "0.064"]]


def test_research_runner_still_needs_no_budget(tmp_path):
    composition = research_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runner = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        result = runner.invoke(proposal(), trace_id="trace-research")
        assert result["status"] == "completed"
        assert runner.production_mode is False


def test_a_product_budget_for_one_task_cannot_cover_another_task(tmp_path):
    composition = product_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runner = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        registered = proposal()
        composition.budgets.register(registered.task_id, ACTOR, Budget())
        with pytest.raises(BudgetExceededError, match="task_budget_access_denied"):
            runner.invoke(proposal(), trace_id="trace-cross-task")
        # Another actor's scope is refused earlier still, by the runner's own
        # actor binding; the budget ledger keeps its independent scope check.
        with pytest.raises(PermissionError, match="rule_runner_actor_scope_mismatch"):
            runner.invoke(
                proposal(
                    task_id=registered.task_id,
                    user_id=OTHER.user_id,
                    tenant_id=OTHER.tenant_id,
                    project_id=OTHER.project_id,
                ),
                trace_id="trace-cross-actor",
            )


def test_a_research_composition_cannot_be_reused_as_a_product_one(tmp_path):
    research = research_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(tmp_path, saver)
        with pytest.raises(ValueError, match="rule_runner_product_composition_requires"):
            GovernedRuleRunner(
                runtime=flow,
                actor=ACTOR,
                artifacts=research.artifacts,
                audit=research.audit,
                composition=ExecutionComposition(
                    mode="product",
                    actor=ACTOR,
                    artifacts=governed_store(tmp_path),
                    audit=ReceiptTransport().sink(),
                    budgets=TaskBudgetLedger(tmp_path / "budgets.sqlite"),
                ),
            )


def _science_fixture(tmp_path):
    from uuid import uuid4

    from generaldataagent.contracts import ExperimentSpec, SnapshotColumn, SplitSpec
    from generaldataagent.data_science.experiment import prepare_experiment
    from generaldataagent.data_science.snapshot import create_dataset_snapshot

    store = LocalArtifactStore(tmp_path / "science-artifacts")
    task_id = uuid4()
    rows = [
        {
            "age": 20 + index,
            "score": (index * 7) % 19,
            "group": f"g{index % 4}",
            "converted": "yes" if index % 3 == 0 else "no",
        }
        for index in range(20)
    ]
    columns = (
        SnapshotColumn(name="age", data_type="integer", role="feature"),
        SnapshotColumn(name="score", data_type="integer", role="feature"),
        SnapshotColumn(name="group", data_type="string", role="group"),
        SnapshotColumn(name="converted", data_type="string", role="target"),
    )
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domain="payments",
        source_name="composition-fixture",
        rows=rows,
        columns=columns,
        artifact_store=store,
    )
    spec = ExperimentSpec(
        task_id=task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type="binary_classification",
        target_column="converted",
        feature_columns=("age", "score"),
        baseline="majority",
        primary_metric="f1",
        split=SplitSpec(),
    )
    assert prepare_experiment(rows, spec).blocked is False
    return store, snapshot, spec


def test_science_workbench_refuses_a_composition_that_does_not_match_its_ports(tmp_path):
    from generaldataagent.data_science import DataScienceWorkbench

    store, snapshot, spec = _science_fixture(tmp_path)
    other = research_composition(tmp_path)
    with pytest.raises(ValueError, match="science_composition_ports_disagree"):
        DataScienceWorkbench(artifact_store=store, composition=other)
    matching = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=store,
        audit=JsonlAuditSink(tmp_path / "matching-audit.jsonl"),
    )
    with pytest.raises(ValueError, match="science_production_mode_requires_a_product"):
        DataScienceWorkbench(
            artifact_store=store,
            audit=matching.audit,
            composition=matching,
            production_mode=True,
        )


def test_science_workbench_refuses_a_request_outside_the_composition_actor(tmp_path):
    from generaldataagent.data_science import DataScienceWorkbench

    store, snapshot, spec = _science_fixture(tmp_path)
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=store,
        audit=JsonlAuditSink(tmp_path / "science-audit.jsonl"),
    )
    workbench = DataScienceWorkbench(artifact_store=store, composition=composition)
    with pytest.raises(PermissionError, match="science_composition_actor_mismatch"):
        workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id="someone-else",
            tenant_id=ACTOR.tenant_id,
            project_id=ACTOR.project_id,
            data_domain="payments",
        )
    report = workbench.run_experiment(
        snapshot=snapshot,
        spec=spec,
        user_id=ACTOR.user_id,
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domain="payments",
    )
    assert report.passed is True


def test_science_cannot_run_under_an_unregistered_task_budget(tmp_path):
    from generaldataagent.contracts import (
        ExperimentSpec,
        OCIContainerRuntime,
        SnapshotColumn,
    )
    from generaldataagent.data_science import DataScienceWorkbench
    from generaldataagent.data_science.snapshot import create_dataset_snapshot

    composition = ExecutionComposition(
        mode="product",
        actor=ACTOR,
        artifacts=governed_store(tmp_path / "science"),
        audit=ReceiptTransport().sink(),
        budgets=TaskBudgetLedger(tmp_path / "science-budgets.sqlite"),
    )
    task_id = uuid4()
    rows = [
        {
            "age": 20 + index,
            "score": (index * 7) % 19,
            "group": f"g{index % 4}",
            "converted": "yes" if index % 3 == 0 else "no",
        }
        for index in range(20)
    ]
    columns = (
        SnapshotColumn(name="age", data_type="integer", role="feature"),
        SnapshotColumn(name="score", data_type="integer", role="feature"),
        SnapshotColumn(name="group", data_type="string", role="group"),
        SnapshotColumn(name="converted", data_type="string", role="target"),
    )
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domain="payments",
        source_name="composition-fixture",
        rows=rows,
        columns=columns,
        artifact_store=composition.artifacts,
    )
    spec = ExperimentSpec(
        task_id=task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type="binary_classification",
        target_column="converted",
        feature_columns=("age", "score"),
        baseline="majority",
        primary_metric="f1",
    )
    # A production workbench also demands the OCI sandbox; the container never
    # starts because the budget check rejects first.
    from generaldataagent.data_science.sandbox import OCIContainerSandbox

    workbench = DataScienceWorkbench(
        artifact_store=composition.artifacts,
        audit=composition.audit,
        sandbox=OCIContainerSandbox(
            OCIContainerRuntime(
                engine="docker",
                image_ref=f"registry.example/gda-stage3@sha256:{'e' * 64}",
            )
        ),
        composition=composition,
    )
    with pytest.raises(BudgetExceededError, match="task_budget_access_denied"):
        workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id=ACTOR.user_id,
            tenant_id=ACTOR.tenant_id,
            project_id=ACTOR.project_id,
            data_domain="payments",
        )
    composition.budgets.register(task_id, ACTOR, Budget())
    # Registered now, so the budget gate passes and the audit event is written;
    # the experiment then stops at the sandbox rather than at the budget.
    events = []
    original = composition.audit.transport
    composition.audit.transport = lambda content: (
        events.append(json.loads(content)),
        original(content),
    )[1]
    with pytest.raises(Exception) as failure:
        workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id=ACTOR.user_id,
            tenant_id=ACTOR.tenant_id,
            project_id=ACTOR.project_id,
            data_domain="payments",
        )
    assert not isinstance(failure.value, BudgetExceededError)
    assert [event["payload"]["decision"] for event in events] == ["allow"]


def test_one_artifact_authorization_boundary_serves_every_writer(tmp_path):
    from generaldataagent.artifacts.governed import ArtifactScopeError, require_artifact_scope

    governed = governed_store(tmp_path)
    # A plain content-addressed store carries no scope, so nothing can mismatch.
    assert require_artifact_scope(LocalArtifactStore(tmp_path / "plain"), OTHER) is None
    assert require_artifact_scope(governed, ACTOR) is None
    # The store's domain set must cover the actor's.
    assert require_artifact_scope(governed, ACTOR) is None
    broad_store = governed_store(
        tmp_path / "broad", actor=ACTOR.model_copy(update={"data_domains": ("payments", "extra")})
    )
    assert require_artifact_scope(broad_store, ACTOR) is None
    for change in (
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("sales",)},
    ):
        with pytest.raises(ArtifactScopeError, match="artifact_store_actor_scope_mismatch"):
            require_artifact_scope(governed, ACTOR.model_copy(update=change))
    wider = ACTOR.model_copy(update={"data_domains": ("payments", "extra")})
    with pytest.raises(ArtifactScopeError, match="artifact_store_actor_scope_mismatch"):
        require_artifact_scope(governed, wider)


def pseudonym(sink, value: str) -> str:
    """The independent audit service sees only the keyed pseudonym of a trace."""
    import hmac

    return hmac.new(sink.pseudonym_key, value.encode(), hashlib.sha256).hexdigest()


def test_composed_task_and_trace_are_not_the_callers_to_choose(tmp_path):
    composition = product_composition(tmp_path, task_id=TASK, trace_id="trace-composed")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runner = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        # Someone else's task id cannot borrow this composition's ports.
        with pytest.raises(PermissionError, match="rule_runner_task_scope_mismatch"):
            runner.invoke(proposal(), trace_id="trace-caller")
        composed = proposal(task_id=TASK)
        composition.budgets.register(TASK, ACTOR, Budget())
        result = runner.invoke(composed, trace_id="trace-caller")
        assert result["status"] == "completed"
        # Every audited decision carries the composed trace, not the caller's.
        traces = {event["trace_id"] for event in composition.audit.transport.events}
        assert traces == {pseudonym(composition.audit, "trace-composed")}
        assert pseudonym(composition.audit, "trace-caller") not in traces


def test_delivery_refuses_a_task_outside_the_composition(tmp_path):
    composition = product_composition(tmp_path, task_id=TASK, trace_id="trace-delivery")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(tmp_path, saver)
        request = RuleAnalysisRequest(operation="average_fee", semantics_excerpt="null means all")
        thread, _ = flow.run(request, ACTOR, thread_id=str(TASK))
        composition.budgets.register(TASK, ACTOR, Budget())
        service = RuleDeliveryRuntime(
            workflow=flow,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        with pytest.raises(PermissionError, match="rule_delivery_task_scope_mismatch"):
            service.deliver(thread, request, ACTOR, task_id=uuid4())
        result = service.deliver(thread, request, ACTOR, task_id=TASK)
        assert result["task_id"] == str(TASK)
        traces = {event["trace_id"] for event in composition.audit.transport.events}
        assert traces == {pseudonym(composition.audit, "trace-delivery")}


def test_science_refuses_a_task_outside_the_composition(tmp_path):
    from generaldataagent.data_science import DataScienceWorkbench

    store, snapshot, spec = _science_fixture(tmp_path)
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=store,
        audit=JsonlAuditSink(tmp_path / "science-audit.jsonl"),
        task_id=uuid4(),
        trace_id="trace-science",
    )
    workbench = DataScienceWorkbench(artifact_store=store, composition=composition)
    with pytest.raises(PermissionError, match="science_composition_task_mismatch"):
        workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id=ACTOR.user_id,
            tenant_id=ACTOR.tenant_id,
            project_id=ACTOR.project_id,
            data_domain="payments",
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"task_id": "not-a-uuid"}, "composition_task_id_invalid"),
        ({"trace_id": ""}, "composition_trace_id_invalid"),
        ({"trace_id": "   "}, "composition_trace_id_invalid"),
        ({"trace_id": 7}, "composition_trace_id_invalid"),
    ],
)
def test_composed_identity_fields_are_validated(tmp_path, change, code):
    values = (
        dict(
            mode="research",
            actor=ACTOR,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        )
        | change
    )
    with pytest.raises(CompositionError, match=code):
        ExecutionComposition(**values)


def _metric_fixture(
    tmp_path,
    *,
    artifacts,
    audit,
    budgets,
    checkpointer=None,
    composition=None,
    production_mode=False,
):
    import sqlite3

    from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
    from generaldataagent.orchestration import MetricAnalysisRuntime
    from generaldataagent.tools import SQLiteReadOnlyExecutor

    database = tmp_path / "orders.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS orders (created_at TEXT, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [("2026-10-01", "web"), ("2026-10-02", "store")],
        )
    metric = MetricDefinition(
        name="order_count",
        version="v1",
        description="orders",
        data_domain="payments",
        relation="orders",
        expression="COUNT(*)",
        time_column="created_at",
        owner="data",
    )
    return MetricAnalysisRuntime(
        catalog=InMemoryMetricCatalog([metric]),
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=artifacts,
        audit=audit,
        budgets=budgets,
        checkpointer=checkpointer,
        composition=composition,
        production_mode=production_mode,
    )


def _metric_query():
    from generaldataagent.contracts import MetricQuery

    return MetricQuery(metric_name="order_count", metric_version="v1")


def test_metric_runtime_refuses_ports_that_disagree_with_its_composition(tmp_path):
    composition = product_composition(tmp_path)
    with pytest.raises(ValueError, match="analysis_composition_ports_disagree"):
        _metric_fixture(
            tmp_path,
            artifacts=LocalArtifactStore(tmp_path / "other"),
            audit=composition.audit,
            budgets=composition.budgets,
            composition=composition,
        )
    # A research composition cannot be relabelled as a production runtime.
    research = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=composition.artifacts,
        audit=JsonlAuditSink(tmp_path / "research.jsonl"),
    )
    with pytest.raises(ValueError, match="analysis_production_mode_requires_a_product"):
        _metric_fixture(
            tmp_path,
            artifacts=composition.artifacts,
            audit=research.audit,
            budgets=None,
            composition=research,
            production_mode=True,
        )
    # Two ledgers would meter one task twice.
    with pytest.raises(ValueError, match="analysis_composition_budgets_disagree"):
        _metric_fixture(
            tmp_path,
            artifacts=composition.artifacts,
            audit=composition.audit,
            budgets=TaskBudgetLedger(tmp_path / "second.sqlite"),
            composition=composition,
        )


def test_metric_runtime_refuses_a_composition_for_another_actor(tmp_path):
    composition = ExecutionComposition(
        mode="research",
        actor=OTHER,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
    )
    runtime = _metric_fixture(
        tmp_path,
        artifacts=composition.artifacts,
        audit=composition.audit,
        budgets=None,
        composition=composition,
    )
    with pytest.raises(PermissionError, match="analysis_composition_actor_mismatch"):
        runtime.run(_metric_query(), ACTOR)


def test_metric_runtime_runs_only_under_the_composed_task_and_trace(tmp_path):
    budgets = TaskBudgetLedger(tmp_path / "budgets.sqlite")
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        budgets=budgets,
        task_id=TASK,
        trace_id="trace-metric",
    )
    runtime = _metric_fixture(
        tmp_path,
        artifacts=composition.artifacts,
        audit=composition.audit,
        budgets=budgets,
        composition=composition,
    )
    # The composed task owns its budget; nothing registers a default for it.
    with pytest.raises(BudgetExceededError, match="task_budget_access_denied"):
        runtime.run(_metric_query(), ACTOR)
    budgets.register(TASK, ACTOR, Budget())
    result = runtime.run(_metric_query(), ACTOR)
    assert result["task_id"] == str(TASK)
    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert {event["trace_id"] for event in events} == {"trace-metric"}
    assert {event["task_id"] for event in events} == {str(TASK)}
    # Evidence lands in the composed task, not an anonymous one.
    assert [path.name for path in (tmp_path / "artifacts").iterdir()] == [str(TASK)]


def test_metric_resume_refuses_a_task_outside_the_composition(tmp_path):
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        task_id=TASK,
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = _metric_fixture(
            tmp_path,
            artifacts=composition.artifacts,
            audit=composition.audit,
            budgets=None,
            checkpointer=saver,
            composition=composition,
        )
        result = runtime.run(_metric_query(), ACTOR)
        assert result["task_id"] == str(TASK)
        assert runtime.resume(result["task_id"], ACTOR)["task_id"] == str(TASK)
        # The same actor's thread for another task is not this composition's
        # work to finish, even though the checkpoint scope would allow it.
        plain = _metric_fixture(
            tmp_path,
            artifacts=composition.artifacts,
            audit=composition.audit,
            budgets=None,
            checkpointer=saver,
        )
        foreign = plain.run(_metric_query(), ACTOR)["task_id"]
        assert foreign != str(TASK)
        with pytest.raises(PermissionError, match="analysis_composition_task_mismatch"):
            runtime.resume(foreign, ACTOR)
        # A different actor is refused earlier, by the checkpoint's own scope.
        with pytest.raises(PermissionError, match="checkpoint_access_denied"):
            runtime.resume(result["task_id"], ACTOR.model_copy(update={"data_domains": ("other",)}))


def test_identity_distinguishes_a_covering_actor_from_the_same_principal(tmp_path):
    from generaldataagent.orchestration import ExecutionIdentity

    identity = ExecutionIdentity(actor=ACTOR)
    # Science admits a narrower actor inside the bound scope; delivery does not.
    broad = ExecutionIdentity(
        actor=ACTOR.model_copy(update={"data_domains": ("payments", "extra")})
    )
    narrow = ACTOR.model_copy(update={"data_domains": ("payments",)})
    broad.require_covering_actor(narrow, "covering_refused")
    with pytest.raises(PermissionError, match="equal_refused"):
        broad.require_equal_actor(narrow, "equal_refused")
    for change in (
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"user_id": "other"},
        {"data_domains": ("payments", "extra")},
    ):
        wider = ACTOR.model_copy(update=change)
        with pytest.raises(PermissionError, match="covering_refused"):
            identity.require_covering_actor(wider, "covering_refused")


def test_identity_resolves_the_composed_task_and_trace_or_keeps_the_callers():
    from generaldataagent.orchestration import ExecutionIdentity

    # An identity that owns neither leaves the caller's choice alone.
    loose = ExecutionIdentity(actor=ACTOR)
    assert loose.resolve_task(None, code="task_refused") is None
    assert loose.resolve_task(TASK, code="task_refused") == TASK
    assert loose.resolve_trace("caller-trace") == "caller-trace"
    # A composed one is not the caller's to override, and fills in what they omit.
    bound = ExecutionIdentity(actor=ACTOR, task_id=TASK, trace_id="composed")
    assert bound.resolve_task(None, code="task_refused") == TASK
    assert bound.resolve_task(TASK, code="task_refused") == TASK
    with pytest.raises(PermissionError, match="task_refused"):
        bound.resolve_task(uuid4(), code="task_refused")
    assert bound.resolve_trace("caller-trace") == "composed"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"actor": "not-an-actor"}, "identity_actor_required"),
        ({"task_id": "not-a-uuid"}, "identity_task_id_invalid"),
        ({"trace_id": "  "}, "identity_trace_id_invalid"),
    ],
)
def test_identity_validates_its_own_fields(change, code):
    from generaldataagent.orchestration import ExecutionIdentity

    with pytest.raises(ValueError, match=code):
        ExecutionIdentity(**({"actor": ACTOR} | change))


def test_delivery_uses_the_composed_audit_sink_when_none_is_passed(tmp_path):
    """A composition declares the audit port; omitting the kwarg must not silence it."""
    composition = research_composition(tmp_path)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(tmp_path, saver)
        request = RuleAnalysisRequest(operation="average_fee", semantics_excerpt="null means all")
        thread, _ = flow.run(request, ACTOR, thread_id=str(TASK))
        # No ``audit=``: the composition's sink must be adopted, not abandoned.
        service = RuleDeliveryRuntime(
            workflow=flow, artifacts=composition.artifacts, composition=composition
        )
        result = service.deliver(thread, request, ACTOR, task_id=TASK)
        assert result["task_id"] == str(TASK)
        assert composition.audit.path.exists()
        events = [
            json.loads(line)
            for line in composition.audit.path.read_text().splitlines()
            if line.strip()
        ]
        assert {event["event_type"] for event in events} >= {"policy.decision"}


def test_a_composed_runtime_cannot_finish_another_actors_thread(tmp_path):
    """The composed runtime must be the principal, not merely the thread's friend."""
    from generaldataagent.artifacts.governed import GovernedArtifactStore
    from generaldataagent.contracts import MetricQuery
    from generaldataagent.observability import JsonlAuditSink

    alice = ACTOR
    bob = ACTOR.model_copy(update={"user_id": "someone-else"})
    alice_store = GovernedArtifactStore(tmp_path / "alice", index_path=tmp_path / "ia", actor=alice)
    from generaldataagent.orchestration.checkpoints import open_protected_checkpointer

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        # The graph is compiled with its saver, so the plain runtime must be
        # built with the shared one rather than assigned it after the fact.
        plain = _metric_fixture(
            tmp_path, artifacts=alice_store, audit=None, budgets=None, checkpointer=saver
        )
        thread = plain.run(
            MetricQuery(metric_name="order_count", metric_version="v1"), alice, thread_id=None
        )["task_id"]

        bob_store = GovernedArtifactStore(tmp_path / "bob", index_path=tmp_path / "ib", actor=bob)
        bob_sink = JsonlAuditSink(tmp_path / "bob-audit.jsonl")
        bob_composition = ExecutionComposition(
            mode="research", actor=bob, artifacts=bob_store, audit=bob_sink, trace_id="bob-trace"
        )
        bob_runtime = _metric_fixture(
            tmp_path,
            artifacts=bob_store,
            audit=bob_sink,
            budgets=None,
            checkpointer=saver,
            composition=bob_composition,
        )
        with pytest.raises(PermissionError, match="analysis_composition_actor_mismatch"):
            bob_runtime.resume(thread, alice)


def test_a_composed_ledger_without_a_composed_task_is_refused(tmp_path):
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        budgets=TaskBudgetLedger(tmp_path / "budgets.sqlite"),
    )
    with pytest.raises(ValueError, match="analysis_composition_budgets_require_a_task"):
        _metric_fixture(
            tmp_path,
            artifacts=composition.artifacts,
            audit=composition.audit,
            budgets=None,
            composition=composition,
        )
