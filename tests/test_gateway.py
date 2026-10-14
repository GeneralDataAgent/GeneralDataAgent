from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.contracts import (
    AgentRole,
    ArtifactClassification,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.policy import (
    ApprovalError,
    InMemoryApprovalStore,
    PolicyEngine,
    SQLiteApprovalStore,
)
from generaldataagent.tools import PolicyDeniedError, ToolGateway


def test_gateway_cannot_bypass_policy() -> None:
    gateway = ToolGateway(PolicyEngine())
    called = False

    def handler(arguments):
        nonlocal called
        called = True
        return arguments

    gateway.register("production.write", handler)
    request = ToolRequest(
        task_id=uuid4(),
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.DATA_ENGINEER,
        tool_name="production.write",
        action="update",
        resource="warehouse.prod.orders",
        data_domains=("commerce",),
    )
    with pytest.raises(PolicyDeniedError, match="require_approval"):
        gateway.invoke(request, trace_id="trace-1")
    assert called is False


def test_gateway_consumes_exact_approval_once() -> None:
    approvals = InMemoryApprovalStore()
    gateway = ToolGateway(PolicyEngine(), approvals=approvals)
    calls: list[dict] = []
    gateway.register("dataset.export", lambda arguments: calls.append(arguments) or "exported")
    request = ToolRequest(
        task_id=uuid4(),
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"format": "csv", "limit": 100},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    approval_request = approvals.create_request(
        request,
        impact_scope="Export at most 100 commerce rows to an internal artifact.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact before delivery.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    approvals.approve(approval_request.approval_id, approver_id="reviewer-1")

    result = gateway.invoke(
        request,
        trace_id="approved-export",
        approval_id=approval_request.approval_id,
    )

    assert result == "exported"
    assert calls == [{"format": "csv", "limit": 100}]
    with pytest.raises(ApprovalError, match="already been consumed"):
        gateway.invoke(
            request,
            trace_id="replayed-export",
            approval_id=approval_request.approval_id,
        )


def test_gateway_rejects_changed_action_after_approval() -> None:
    approvals = InMemoryApprovalStore()
    gateway = ToolGateway(PolicyEngine(), approvals=approvals)
    called = False

    def handler(arguments):
        nonlocal called
        called = True
        return arguments

    gateway.register("dataset.export", handler)
    request = ToolRequest(
        task_id=uuid4(),
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"limit": 100},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    approval_request = approvals.create_request(
        request,
        impact_scope="Export at most 100 commerce rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    approvals.approve(approval_request.approval_id, approver_id="reviewer-1")
    changed_request = request.model_copy(update={"arguments": {"limit": 1_000}})

    with pytest.raises(ApprovalError, match="does not match final request"):
        gateway.invoke(
            changed_request,
            trace_id="changed-export",
            approval_id=approval_request.approval_id,
        )
    assert called is False


def test_approval_store_rejects_self_approval() -> None:
    approvals = InMemoryApprovalStore()
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"limit": 10},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    approval_request = approvals.create_request(
        request,
        impact_scope="Export at most 10 rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    with pytest.raises(ApprovalError, match="cannot approve their own"):
        approvals.approve(approval_request.approval_id, approver_id="requester-1")


def test_sqlite_approval_survives_restart_and_still_consumes_once(tmp_path) -> None:
    database = tmp_path / "approvals.sqlite"
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"format": "csv", "limit": 25},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    first_process = SQLiteApprovalStore(database)
    approval_request = first_process.create_request(
        request,
        impact_scope="Export at most 25 internal rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    first_process.approve(approval_request.approval_id, approver_id="reviewer-1")

    after_restart = SQLiteApprovalStore(database)
    gateway = ToolGateway(PolicyEngine(), approvals=after_restart)
    gateway.register("dataset.export", lambda arguments: arguments["limit"])
    result = gateway.invoke(
        request,
        trace_id="resumed-export",
        approval_id=approval_request.approval_id,
    )

    assert result == 25
    after_second_restart = SQLiteApprovalStore(database)
    replay_gateway = ToolGateway(PolicyEngine(), approvals=after_second_restart)
    replay_gateway.register("dataset.export", lambda arguments: arguments["limit"])
    with pytest.raises(ApprovalError, match="already been consumed"):
        replay_gateway.invoke(
            request,
            trace_id="replayed-after-restart",
            approval_id=approval_request.approval_id,
        )


def test_rejected_approval_cannot_be_granted_or_consumed() -> None:
    approvals = InMemoryApprovalStore()
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"limit": 50},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    approval_request = approvals.create_request(
        request,
        impact_scope="Export at most 50 internal rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    disposition = approvals.reject(
        approval_request.approval_id,
        reviewer_id="reviewer-1",
        reason="The requested export is not necessary.",
    )

    assert disposition.disposition == "rejected"
    with pytest.raises(ApprovalError, match="already closed"):
        approvals.approve(approval_request.approval_id, approver_id="reviewer-2")
    with pytest.raises(ApprovalError, match="was rejected"):
        approvals.consume(approval_request.approval_id, request)


def test_approved_action_can_be_cancelled_and_stays_cancelled_after_restart(tmp_path) -> None:
    database = tmp_path / "cancelled-approvals.sqlite"
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"limit": 25},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    store = SQLiteApprovalStore(database)
    approval_request = store.create_request(
        request,
        impact_scope="Export at most 25 internal rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.approve(approval_request.approval_id, approver_id="reviewer-1")
    disposition = store.cancel(
        approval_request.approval_id,
        requester_id="requester-1",
        reason="The requester no longer needs the export.",
    )

    assert disposition.disposition == "cancelled"
    after_restart = SQLiteApprovalStore(database)
    with pytest.raises(ApprovalError, match="was cancelled"):
        after_restart.consume(approval_request.approval_id, request)


def test_expired_grant_stays_unusable_after_restart_with_injected_clock(tmp_path) -> None:
    database = tmp_path / "expired-approvals.sqlite"
    issued_at = datetime.now(UTC)
    current_time = [issued_at]
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.REPORTER,
        tool_name="dataset.export",
        action="export",
        resource="warehouse.analytics.orders",
        arguments={"limit": 25},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    store = SQLiteApprovalStore(database, clock=lambda: current_time[0])
    approval_request = store.create_request(
        request,
        impact_scope="Export at most 25 internal rows.",
        cost_estimate="Less than one minute of compute.",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Delete the generated artifact.",
        expires_at=issued_at + timedelta(minutes=5),
    )
    store.approve(approval_request.approval_id, approver_id="reviewer-1")
    current_time[0] = issued_at + timedelta(minutes=6)

    after_restart = SQLiteApprovalStore(database, clock=lambda: current_time[0])
    with pytest.raises(ApprovalError, match="has expired"):
        after_restart.consume(approval_request.approval_id, request)
