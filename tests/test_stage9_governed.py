import json
from dataclasses import replace
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, ArtifactRef, ToolRequest
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import PolicyEngine
from generaldataagent.policy.engine import DEFAULT_RULES
from generaldataagent.tools.gateway import PolicyDeniedError

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("synthetic",))


def request(**changes):
    values = dict(
        task_id=uuid4(),
        user_id="u",
        tenant_id="t",
        project_id="p",
        agent_role="data_analyst",
        tool_name="rules.execute",
        action="run",
        resource="rule-workflow",
        data_domains=("synthetic",),
        arguments=dict(operation="average_fee", semantics_excerpt="null means all"),
    )
    return ToolRequest(**(values | changes))


def runner(saver, tmp_path, policy=None):
    adapter = AnnexWorkflowServices(
        [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")],
        documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
    )
    flow = RuleWorkflowRuntime(
        services=adapter.services(),
        checkpointer=saver,
        source_hashes=adapter.source_hashes,
        workflow_version="test-v1",
    )
    return GovernedRuleRunner(
        runtime=flow,
        actor=ACTOR,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        policy=policy,
    )


def test_rule_gateway_audits_execution_and_stores_task_bound_evidence(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        proposal = request()
        result = service.invoke(proposal, trace_id="trace-rule")
        assert result["status"] == "completed"
        ref = ArtifactRef.model_validate(result["evidence"])
        assert ref.task_id == proposal.task_id
        evidence = json.loads(service.artifacts.read_bytes(ref))
        assert evidence["delivery"]["average_fee"] == "0.064"
        assert evidence["verification"]["passed"] is True
        assert evidence["source_hashes"]["annex"].startswith("sha256:")
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert len(rows) == 2
        assert all(row["task_id"] == str(proposal.task_id) for row in rows)
        assert all(row["payload"]["decision"] == "allow" for row in rows)


@pytest.mark.parametrize("changes", [dict(agent_role="reporter"), dict(data_domains=())])
def test_policy_denial_prevents_checkpoint_and_artifact_creation(tmp_path, changes):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        proposal = request(**changes)
        with pytest.raises(PolicyDeniedError):
            service.invoke(proposal, trace_id="denied")
        assert not list((tmp_path / "artifacts").iterdir())
        with pytest.raises(PermissionError):
            saver.authorize_thread(str(proposal.task_id), ACTOR)
        rows = (tmp_path / "audit.jsonl").read_text().splitlines()
        assert json.loads(rows[0])["payload"]["decision"] == "deny"


@pytest.mark.parametrize("failed_event", [1, 2])
def test_audit_outage_prevents_handler_or_evidence_write(tmp_path, failed_event):
    class FailingAudit(JsonlAuditSink):
        calls = 0

        def emit(self, event):
            self.calls += 1
            if self.calls == failed_event:
                raise OSError("synthetic audit outage")
            super().emit(event)

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        service.audit = FailingAudit(tmp_path / "audit.jsonl")
        proposal = request()
        with pytest.raises(OSError, match="synthetic audit outage"):
            service.invoke(proposal, trace_id="audit-outage")
        assert not list(service.artifacts.root.iterdir())
        if failed_event == 1:
            with pytest.raises(PermissionError):
                saver.authorize_thread(str(proposal.task_id), ACTOR)
        else:
            # Execution happened before the evidence audit failed. No reference
            # was returned, but its checkpoint must not be described as absent.
            saver.authorize_thread(str(proposal.task_id), ACTOR)


def test_evidence_policy_denial_keeps_checkpoint_but_writes_no_artifact(tmp_path):
    policy = PolicyEngine(
        rules=[rule for rule in DEFAULT_RULES if rule.tool_name != "artifact.write"]
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path, policy=policy)
        proposal = request()
        with pytest.raises(PolicyDeniedError):
            service.invoke(proposal, trace_id="artifact-denied")
        saver.authorize_thread(str(proposal.task_id), ACTOR)
        assert not list(service.artifacts.root.iterdir())
        events = [json.loads(line) for line in service.audit.path.read_text().splitlines()]
        assert [event["payload"]["decision"] for event in events] == ["allow", "deny"]


@pytest.mark.parametrize("role", ["data_analyst", "verifier"])
@pytest.mark.parametrize("allow_caller", [False, True])
def test_evidence_write_uses_original_caller_role(tmp_path, role, allow_caller):
    from generaldataagent.policy import request_fingerprint

    policy = PolicyEngine(
        rules=[
            replace(rule, allowed_roles=frozenset({role if allow_caller else "reporter"}))
            if rule.tool_name == "artifact.write"
            else rule
            for rule in DEFAULT_RULES
        ]
    )
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path, policy=policy)
        proposal = request(agent_role=role)
        if allow_caller:
            result = service.invoke(proposal, trace_id="caller-role")
            ref = ArtifactRef.model_validate(result["evidence"])
            evidence = json.loads(service.artifacts.read_bytes(ref))
        else:
            with pytest.raises(PolicyDeniedError):
                service.invoke(proposal, trace_id="caller-role")
            assert not list(service.artifacts.root.iterdir())
            state = service.runtime.state_of(
                str(proposal.task_id),
                RuleAnalysisRequest.model_validate(proposal.arguments),
                ACTOR,
            )
            assert state["status"] == "completed"
            evidence = {
                "task_id": str(proposal.task_id),
                "thread_id": str(proposal.task_id),
                "request": state["request"],
                **{
                    key: state.get(key)
                    for key in (
                        "source_hashes",
                        "binding_hash",
                        "status",
                        "verification",
                        "computation",
                        "delivery",
                    )
                },
            }
        artifact_request = proposal.model_copy(
            update=dict(
                tool_name="artifact.write",
                action="create",
                resource="rule-workflow-evidence",
                arguments=evidence,
            )
        )
        events = [json.loads(line) for line in service.audit.path.read_text().splitlines()]
        assert len(events) == 2
        assert events[1]["payload"]["request_fingerprint"] == request_fingerprint(artifact_request)
        assert events[1]["payload"]["decision"] == ("allow" if allow_caller else "deny")


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "project_id", "data_domains"])
def test_all_actor_scope_mismatches_prevent_execution(tmp_path, field):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        value = ("outside",) if field == "data_domains" else "outside"
        proposal = request(**{field: value})
        with pytest.raises(PermissionError, match="scope_mismatch"):
            service.invoke(proposal, trace_id="scope-denied")
        assert not list(service.artifacts.root.iterdir())
        assert not service.audit.path.exists()
        with pytest.raises(PermissionError):
            saver.authorize_thread(str(proposal.task_id), ACTOR)


def test_unrecognized_policy_and_cross_tenant_requests_cannot_execute(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path, policy=PolicyEngine(rules=()))
        with pytest.raises(PolicyDeniedError):
            service.invoke(request(), trace_id="unknown")
        with pytest.raises(PermissionError, match="scope_mismatch"):
            service.invoke(request(tenant_id="other"), trace_id="cross-tenant")
        assert not list((tmp_path / "artifacts").iterdir())
