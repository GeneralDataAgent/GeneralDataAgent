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
