import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from test_stage5_governance import ACTOR, HASH, admitted_registry, capability

from generaldataagent.business.optimization import (
    GovernedModelExecutor,
    ModelRoute,
    ProviderResult,
    RoutingRequest,
    ScopedResponseCache,
)
from generaldataagent.contracts import Budget
from generaldataagent.policy.budgets import TaskBudgetLedger


def request(**kwargs):
    return RoutingRequest(
        **(
            dict(
                purpose="narrative",
                prompt="Explain the certified result",
                source_hash=HASH,
                prompt_version="v1",
                policy_version="v1",
                authorization_version="v1",
                classification="internal",
                input_token_bound=20,
                output_token_bound=10,
                max_cost_microunits=1000,
                max_latency_ms=1000,
            )
            | kwargs
        )
    )


@pytest.fixture
def model_setup(tmp_path):
    registry = admitted_registry(tmp_path)
    budgets = TaskBudgetLedger(tmp_path / "budgets")
    task = uuid4()
    budgets.register(task, ACTOR, Budget(max_tokens=1000), max_cost_microunits=1000)
    cache = ScopedResponseCache(
        tmp_path / "cache", key=Fernet.generate_key(), clock=lambda: datetime.now(UTC)
    )
    route = ModelRoute(
        "safe-model",
        "v1",
        HASH,
        frozenset({"narrative"}),
        frozenset({"internal"}),
        1,
        2,
        100,
        enabled=True,
    )
    calls = []

    async def provider(req):
        calls.append(req)
        return ProviderResult(text="Certified result explanation", input_tokens=15, output_tokens=5)

    executor = GovernedModelExecutor(
        routes=(route,),
        registry=registry,
        budgets=budgets,
        providers={(route.name, route.version): provider},
        cache=cache,
        authorized=lambda actor, req: actor == ACTOR,
    )
    return executor, task, route, calls


def test_routing_cache_and_reservation(model_setup):
    executor, task, _, calls = model_setup
    first = executor.execute(task, ACTOR, request())
    assert first["reserved_cost_microunits"] == 40 and first["cached"] is False
    assert executor.execute(task, ACTOR, request())["cached"] is True
    assert len(calls) == 1
    for field in ("source_hash", "prompt_version", "policy_version", "authorization_version"):
        value = "sha256:" + "b" * 64 if field == "source_hash" else "v2"
        assert executor.execute(task, ACTOR, request(**{field: value}))["cached"] is False
    assert len(calls) == 5
    # No plaintext payload in protected cache pages.
    assert b"Certified result explanation" not in executor.cache.path.read_bytes()


def test_route_budget_classification_and_revocation(model_setup):
    executor, task, route, calls = model_setup
    for change in (
        {"purpose": "deploy"},
        {"max_cost_microunits": 0},
        {"max_latency_ms": 1},
        {"classification": "public"},
    ):
        with pytest.raises(PermissionError):
            executor.execute(task, ACTOR, request(**change))
    executor.execute(task, ACTOR, request())
    executor.registry.revoke(route.name, route.version)
    with pytest.raises(PermissionError):
        executor.execute(task, ACTOR, request())
    assert len(calls) == 1


def test_cache_expiry_integrity_and_budget_denial(model_setup):
    executor, task, _, calls = model_setup
    executor.execute(task, ACTOR, request())
    executor.cache.clock = lambda: datetime.now(UTC) + timedelta(minutes=6)
    assert executor.cache.purge() == 1
    executor.execute(task, ACTOR, request())
    assert len(calls) == 2
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(executor.cache.path)) as db, db:
        db.execute("UPDATE response_cache SET encrypted=X'00'")
    with pytest.raises(PermissionError, match="integrity"):
        executor.execute(task, ACTOR, request())
    executor.cache = None
    executor.budgets.reserve(task, ACTOR, cost_microunits=900)
    with pytest.raises(PermissionError, match="budget"):
        executor.execute(task, ACTOR, request())


def test_provider_failure_bounds_deadline_and_authorization(model_setup):
    executor, task, route, _ = model_setup

    async def invalid(req):
        return ProviderResult(text="oversized", input_tokens=100, output_tokens=5)

    executor.providers[(route.name, route.version)] = invalid
    with pytest.raises(PermissionError, match="token_bound"):
        executor.execute(task, ACTOR, request())

    async def slow(req):
        await asyncio.sleep(1)
        return ProviderResult(text="late", input_tokens=1, output_tokens=1)

    executor.routes = (replace(route, estimated_latency_ms=1),)
    executor.providers[(route.name, route.version)] = slow
    with pytest.raises(TimeoutError):
        executor.execute(task, ACTOR, request(max_latency_ms=10))
    executor.authorized = lambda actor, req: False
    with pytest.raises(PermissionError, match="access"):
        executor.execute(task, ACTOR, request())


def test_cheapest_admitted_route_and_pin_changes(model_setup):
    executor, task, route, calls = model_setup
    cheaper = capability(name="cheaper-model")
    executor.registry.propose(cheaper)
    for reviewer, discipline in (("q", "quality"), ("s", "security")):
        executor.registry.review(
            cheaper.name, cheaper.version, reviewer_id=reviewer, discipline=discipline
        )
    executor.registry.activate(cheaper.name, cheaper.version)
    executor.routes = (route, replace(route, name=cheaper.name, input_cost_per_token=0))
    executor.providers[(cheaper.name, cheaper.version)] = executor.providers[
        (route.name, route.version)
    ]
    assert executor.execute(task, ACTOR, request())["model"] == cheaper.name
    executor.routes = (replace(route, content_hash="sha256:" + "b" * 64),)
    with pytest.raises(PermissionError):
        executor.execute(task, ACTOR, request())
    assert len(calls) == 1
