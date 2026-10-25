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
DOCUMENTS = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"
ANNEX = [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")]


class ReceiptTransport:
    """Minimal independent audit service: signs each append, can be told to fail."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.now = datetime.now(UTC)
        self.sequence = 0
        self.fail = False

    def __call__(self, content: bytes) -> AuditReceipt:
        if self.fail:
            raise RuntimeError("audit service down")
        self.sequence += 1
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


def product_composition(tmp_path, *, artifacts=None, audit=None, budgets=None):
    transport = ReceiptTransport()
    return ExecutionComposition(
        mode="product",
        actor=ACTOR,
        artifacts=artifacts or governed_store(tmp_path),
        audit=audit or transport.sink(),
        budgets=budgets or TaskBudgetLedger(tmp_path / "budgets.sqlite"),
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
    for change in ({"tenant_id": "other"}, {"project_id": "other"}, {"data_domains": ("sales",)}):
        with pytest.raises(ArtifactScopeError, match="artifact_store_actor_scope_mismatch"):
            require_artifact_scope(governed, ACTOR.model_copy(update=change))
    wider = ACTOR.model_copy(update={"data_domains": ("payments", "extra")})
    with pytest.raises(ArtifactScopeError, match="artifact_store_actor_scope_mismatch"):
        require_artifact_scope(governed, wider)
