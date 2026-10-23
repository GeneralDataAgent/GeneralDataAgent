import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from test_metric_analysis import actor

from generaldataagent.agents.factory import build_supervisor
from generaldataagent.agents.specs import SPECIALIST_SPECS
from generaldataagent.business.governance import Capability, CapabilityRegistry
from generaldataagent.business.optimization import ModelRoute
from generaldataagent.contracts import Budget
from generaldataagent.llm.orchestration import OrchestrationModelConfig, OrchestrationRouter
from generaldataagent.llm.routed import NarrationSettings
from generaldataagent.observability.audit import JsonlAuditSink
from generaldataagent.policy.budgets import TaskBudgetLedger

HASH = "sha256:" + "b" * 64


def prepare(tmp_path, **kwargs):
    registry = CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    config = OrchestrationModelConfig(
        provider=kwargs.pop("provider", "anthropic"), model_name="provider-test"
    )
    for name in ("cheap", "expensive"):
        registry.propose(
            Capability(
                name=name,
                version="v1",
                kind="model",
                content_hash=config.fingerprint,
                owner="operator",
                tenant_id=actor().tenant_id,
                project_id=actor().project_id,
                data_domains=actor().data_domains,
                regression_evidence_hash=HASH,
                security_evidence_hash=HASH,
                rollback_version="disabled",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        for reviewer, discipline in (("quality", "quality"), ("security", "security")):
            registry.review(name, "v1", reviewer_id=reviewer, discipline=discipline)
        registry.activate(name, "v1")
    routes = tuple(
        ModelRoute(
            name,
            "v1",
            config.fingerprint,
            frozenset({"orchestration"}),
            frozenset({"internal"}),
            price,
            price,
            1,
            enabled=True,
        )
        for name, price in (("cheap", 1), ("expensive", 2))
    )
    router = OrchestrationRouter(
        routes=routes,
        configs={(r.name, r.version): config for r in routes},
        registry=registry,
        budgets=TaskBudgetLedger(tmp_path / "budgets"),
        audit=JsonlAuditSink(tmp_path / "audit"),
        settings=NarrationSettings(
            prompt_version="v1", policy_version="v1", output_token_bound=128
        ),
        authorized=lambda a, r: a == actor(),
        authorization_version=lambda a: "acl-v1",
        budget=kwargs.pop("budget", Budget(max_tokens=300000)),
        enabled=lambda: True,
        **kwargs,
    )
    middleware = router.for_task(actor(), thread_id="test", source_hash=HASH)
    request = ModelRequest(model=router.bootstrap_model, messages=[HumanMessage("review metric")])
    return router, middleware, request


def reply(*, tokens=10, output=8):
    return ModelResponse(
        result=[
            AIMessage(
                content="done",
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": output,
                    "total_tokens": tokens + output,
                },
            )
        ]
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_native_deepagents_supervisor_and_specialist_share_budget(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("OPENAI_API_KEY", "local-offline-test")
    router, middleware, _ = prepare(tmp_path, provider=provider)
    assert router.bootstrap_model.cache is False
    calls = []

    async def generate(self, messages, stop=None, run_manager=None, **kwargs):
        assert self.cache is False
        calls.append(kwargs)
        cap = "max_tokens" if provider == "anthropic" else "max_completion_tokens"
        assert kwargs[cap] == 128
        # With every filesystem tool excluded, a model provider may omit the
        # parameter entirely rather than send an empty list.
        names = {t.get("name") or t["function"]["name"] for t in kwargs.get("tools") or ()}
        assert not names & {
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "delete",
            "glob",
            "grep",
            "execute",
        }
        response = reply().result[0]
        if len(calls) == 1:
            response = response.model_copy(
                update={
                    "tool_calls": [
                        {
                            "name": "task",
                            "args": {"description": "Review metric", "subagent_type": "discovery"},
                            "id": "dispatch",
                            "type": "tool_call",
                        }
                    ]
                }
            )
        return ChatResult(generations=[ChatGeneration(message=response)])

    monkeypatch.setattr(
        ChatAnthropic if provider == "anthropic" else ChatOpenAI, "_agenerate", generate
    )
    graph = build_supervisor(
        model=router.bootstrap_model,
        tool_groups={},
        specialist_specs=tuple(s for s in SPECIALIST_SPECS if s.name == "discovery"),
        model_middleware=(middleware,),
    )
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage("Review metric")]}))
    assert result["messages"][-1].content == "done"
    assert len(calls) == 3
    with sqlite3.connect(router.budgets.path) as db:
        assert db.execute("SELECT calls FROM task_budgets").fetchone()[0] == 3
    events = [json.loads(line) for line in router.audit.path.read_text().splitlines()]
    usage = [e for e in events if e["event_type"] == "orchestration.model_usage"]
    assert len(usage) == 3
    assert all(e["payload"]["model"] == "cheap" for e in usage)
    assert "Review metric" not in router.audit.path.read_text()


def test_retry_and_concurrent_calls_keep_reserved_task_budget(tmp_path):
    router, middleware, request = prepare(tmp_path, budget=Budget(max_tool_calls=1))
    again = router.for_task(actor(), thread_id="test", source_hash=HASH)
    assert again.task_id == middleware.task_id
    calls = []

    async def handler(req):
        calls.append(req)
        await asyncio.sleep(0)
        return reply()

    async def run():
        return await asyncio.gather(
            middleware.awrap_model_call(request, handler),
            again.awrap_model_call(request, handler),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    assert len(calls) == 1
    assert sum(isinstance(r, PermissionError) for r in results) == 1
    with pytest.raises(PermissionError, match="budget"):
        asyncio.run(again.awrap_model_call(request, handler))


@pytest.mark.parametrize("failure", ["revocation", "authorization", "usage", "bound", "dlp"])
def test_reject_response_after_revocation_or_invalid_provider_output(tmp_path, failure):
    router, middleware, request = prepare(tmp_path)

    async def handler(req):
        if failure == "revocation":
            router.registry.revoke("cheap", "v1")
        elif failure == "authorization":
            router.authorization_version = lambda a: "acl-v2"
        elif failure == "usage":
            return ModelResponse(result=[AIMessage(content="done")])
        elif failure == "bound":
            return reply(output=129)
        elif failure == "dlp":
            return ModelResponse(
                result=[reply().result[0].model_copy(update={"content": "password=do-not-export"})]
            )
        return reply()

    with pytest.raises(PermissionError):
        asyncio.run(middleware.awrap_model_call(request, handler))
    assert "orchestration.model_usage" not in router.audit.path.read_text()


def test_native_configuration_and_input_are_checked_before_transport(tmp_path):
    router, middleware, request = prepare(tmp_path)
    called = []

    async def handler(req):
        called.append(req)
        return reply()

    bad = request.override(
        messages=[
            HumanMessage(content=[{"type": "image_url", "image_url": "https://example.org/image"}])
        ]
    )
    with pytest.raises(PermissionError, match="text_content"):
        asyncio.run(middleware.awrap_model_call(bad, handler))
    router.enabled = lambda: False
    with pytest.raises(PermissionError, match="admitted"):
        asyncio.run(middleware.awrap_model_call(request, handler))
    assert called == []


def test_transport_failure_is_reserved_and_not_retried_or_cached(tmp_path):
    router, middleware, request = prepare(tmp_path)
    called = []

    async def fail(req):
        called.append(req)
        raise ConnectionError("private transport details")

    with pytest.raises(ConnectionError):
        asyncio.run(middleware.awrap_model_call(request, fail))
    assert len(called) == 1
    with sqlite3.connect(router.budgets.path) as db:
        calls, tokens, cost = db.execute("SELECT calls,tokens,cost FROM task_budgets").fetchone()
    assert calls == 1 and tokens > 0 and cost > 0
    assert "private transport details" not in router.audit.path.read_text()


def test_provider_timeout_does_not_fallback(tmp_path):
    router, middleware, request = prepare(tmp_path)
    router.settings = router.settings.model_copy(update={"max_latency_ms": 1})
    called = []

    async def slow(req):
        called.append(req)
        await asyncio.sleep(1)
        return reply()

    with pytest.raises(TimeoutError):
        asyncio.run(middleware.awrap_model_call(request, slow))
    assert len(called) == 1


def test_endpoint_secrets_rejected_and_environment_cannot_redirect(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        OrchestrationModelConfig(
            provider="openai", model_name="test", base_url="https://user:pass@example.org"
        )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unapproved.example.org")
    router, _, _ = prepare(tmp_path)
    assert str(router.bootstrap_model.anthropic_api_url) == "https://api.anthropic.com"
