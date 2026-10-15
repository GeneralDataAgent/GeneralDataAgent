from uuid import uuid4

from generaldataagent.contracts import AgentRole, RiskLevel, ToolRequest
from generaldataagent.policy import PolicyEngine


def request(**overrides: object) -> ToolRequest:
    values = {
        "task_id": uuid4(),
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "agent_role": AgentRole.DATA_ANALYST,
        "tool_name": "sql.query",
        "action": "query",
        "resource": "warehouse.analytics.orders",
        "data_domains": ("commerce",),
        "requested_risk": RiskLevel.R1,
    }
    values.update(overrides)
    return ToolRequest(**values)


def test_allows_explicit_read_only_query_rule() -> None:
    decision = PolicyEngine().evaluate(request())
    assert decision.decision == "allow"
    assert "read_only" in decision.obligations


def test_fails_closed_for_unknown_tool() -> None:
    decision = PolicyEngine().evaluate(request(tool_name="unknown.tool"))
    assert decision.decision == "deny"
    assert decision.reason_code == "unknown_tool"


def test_requires_approval_for_export() -> None:
    decision = PolicyEngine().evaluate(
        request(
            agent_role=AgentRole.REPORTER,
            tool_name="dataset.export",
            action="export",
            requested_risk=RiskLevel.R2,
        )
    )
    assert decision.decision == "require_approval"


def test_requires_r2_approval_for_dbt_test() -> None:
    decision = PolicyEngine().evaluate(
        request(
            agent_role=AgentRole.DATA_ENGINEER,
            tool_name="dbt.test",
            action="test",
            requested_risk=RiskLevel.R2,
        )
    )
    assert decision.decision == "require_approval"
    assert decision.risk_level == RiskLevel.R2


def test_requires_r2_approval_for_development_write() -> None:
    decision = PolicyEngine().evaluate(
        request(
            agent_role=AgentRole.DATA_ENGINEER,
            tool_name="development.write",
            action="materialize",
            requested_risk=RiskLevel.R2,
        )
    )
    assert decision.decision == "require_approval"
    assert decision.risk_level == RiskLevel.R2
    assert "no_production_write" in decision.obligations


def test_never_allows_permission_changes() -> None:
    decision = PolicyEngine().evaluate(
        request(
            agent_role=AgentRole.SUPERVISOR,
            tool_name="permission.change",
            action="grant",
            requested_risk=RiskLevel.R4,
        )
    )
    assert decision.decision == "deny"
    assert decision.reason_code == "permanently_denied"
