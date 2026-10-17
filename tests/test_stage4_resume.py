from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ArtifactClassification, ToolRequest
from generaldataagent.policy import ApprovalError, PolicyEngine, SQLiteApprovalStore
from generaldataagent.policy.resume import PendingActionCoordinator
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.tools.gateway import ToolGateway


@pytest.mark.parametrize(
    "risk,tool,action",
    [("r2", "development.write", "materialize"), ("r3", "production.write", "deploy")],
)
def test_original_thread_resume_rechecks_identity_approval_and_prevents_replay(
    tmp_path, risk, tool, action
):
    now = datetime.now(UTC)
    broker = SQLiteSessionBroker(tmp_path / "sessions.db", clock=lambda: now)
    approvals = SQLiteApprovalStore(tmp_path / "approvals.db", clock=lambda: now)
    gateway = ToolGateway(PolicyEngine(), approvals=approvals)
    calls = []
    gateway.register(tool, lambda arguments: calls.append(arguments) or {"ok": True})
    scope = dict(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    token = broker.issue(**scope, permissions=frozenset({"actions:resume"}))
    request = ToolRequest(
        task_id=uuid4(),
        **{**scope, "data_domains": ("d",)},
        tool_name=tool,
        action=action,
        resource="dev.resource",
        agent_role="data_engineer",
        requested_risk=risk,
        arguments={"value": 1},
    )
    approval = approvals.create_request(
        request,
        impact_scope="development fixture",
        cost_estimate="zero",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="delete fixture",
        expires_at=now + timedelta(minutes=2),
    )
    path = tmp_path / "pending.db"
    coordinator = PendingActionCoordinator(path, broker=broker, gateway=gateway)
    coordinator.pause("original-thread", request, approval)
    with pytest.raises(ApprovalError):
        coordinator.resume("original-thread", token)
    assert not calls  # Waiting for approval must not consume the pending action.
    approvals.approve(approval.approval_id, approver_id="independent-reviewer")
    for change in (
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": frozenset({"other"})},
    ):
        with pytest.raises(ApprovalError, match="access_denied"):
            coordinator.resume(
                "original-thread",
                broker.issue(**{**scope, **change}, permissions=frozenset({"actions:resume"})),
            )
    with pytest.raises(ApprovalError, match="access_denied"):
        coordinator.resume("different-thread", token)
    coordinator = PendingActionCoordinator(path, broker=broker, gateway=gateway)

    def resume(_):
        try:
            coordinator.resume("original-thread", token)
            return True
        except ApprovalError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(resume, range(16))) == 1
    assert calls == [{"value": 1}]
    with pytest.raises(ApprovalError):
        approvals.consume(approval.approval_id, request)


def test_revoked_session_and_mutated_approval_never_execute(tmp_path):
    broker = SQLiteSessionBroker(tmp_path / "sessions.db")
    approvals = SQLiteApprovalStore(tmp_path / "approvals.db")
    gateway = ToolGateway(PolicyEngine(), approvals=approvals)
    coordinator = PendingActionCoordinator(tmp_path / "pending.db", broker=broker, gateway=gateway)
    request = ToolRequest(
        task_id=uuid4(),
        user_id="u",
        tenant_id="t",
        project_id="p",
        data_domains=("d",),
        tool_name="development.write",
        action="materialize",
        resource="dev.x",
        agent_role="data_engineer",
        requested_risk="r2",
    )
    approval = approvals.create_request(
        request,
        impact_scope="fixture",
        cost_estimate="zero",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="remove",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(ApprovalError):
        coordinator.pause("thread", request.model_copy(update={"resource": "different"}), approval)
    coordinator.pause("thread", request, approval)
    token = broker.issue(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    broker.revoke(token)
    with pytest.raises(PermissionError):
        coordinator.resume("thread", token)
    client = TestClient(create_app(session_broker=broker, pending_actions=coordinator))
    assert client.post("/v1/actions/thread/resume").status_code == 401
    with pytest.raises(ValueError):
        create_app(pending_actions=coordinator)
