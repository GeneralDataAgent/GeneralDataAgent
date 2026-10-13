from uuid import uuid4

import pytest

from generaldataagent.contracts import AgentRole, ToolRequest
from generaldataagent.policy import PolicyEngine
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
