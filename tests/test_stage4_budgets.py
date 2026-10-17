from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.contracts import ActorContext, Budget, ToolRequest
from generaldataagent.policy import PolicyEngine
from generaldataagent.policy.budgets import BudgetExceededError, TaskBudgetLedger
from generaldataagent.tools.gateway import ToolGateway


def test_atomic_shared_budgets_cannot_reset_on_restart_or_concurrent_calls(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / "budgets"
    ledger = TaskBudgetLedger(path, clock=lambda: now)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    task = uuid4()
    budget = Budget(max_tool_calls=3, max_tokens=10, max_scan_bytes=20, max_runtime_seconds=10)
    ledger.register(task, actor, budget, max_cost_microunits=30)

    def reserve(_):
        try:
            TaskBudgetLedger(path, clock=lambda: now).reserve(
                task, actor, tokens=3, scan_bytes=6, cost_microunits=10
            )
            return True
        except BudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(16))) == 3
    ledger.register(task, actor, budget, max_cost_microunits=30)
    with pytest.raises(BudgetExceededError):
        ledger.reserve(task, actor)
    with pytest.raises(BudgetExceededError):
        ledger.register(task, actor, Budget())
    with pytest.raises(BudgetExceededError):
        ledger.reserve(task, actor.model_copy(update={"tenant_id": "other"}))
    for amount in (-1, True, 0.5):
        with pytest.raises(ValueError):
            ledger.reserve(task, actor, cost_microunits=amount)
    second = uuid4()
    ledger.register(second, actor, budget)
    now += timedelta(seconds=10)
    with pytest.raises(BudgetExceededError, match="runtime"):
        ledger.reserve(second, actor)


def test_budget_exhaustion_stops_gateway_before_tool_execution(tmp_path):
    ledger = TaskBudgetLedger(tmp_path / "budgets")
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    task = uuid4()
    ledger.register(task, actor, Budget(max_tool_calls=1))
    gateway = ToolGateway(PolicyEngine(), budgets=ledger)
    calls = []
    gateway.register("sql.query", lambda args: calls.append(args))
    request = ToolRequest(
        task_id=task,
        user_id="u",
        tenant_id="t",
        project_id="p",
        data_domains=("d",),
        tool_name="sql.query",
        action="query",
        agent_role="data_analyst",
        resource="orders",
    )
    gateway.invoke(request, trace_id="trace")
    with pytest.raises(BudgetExceededError):
        gateway.invoke(request, trace_id="trace")
    assert calls == [{}]
