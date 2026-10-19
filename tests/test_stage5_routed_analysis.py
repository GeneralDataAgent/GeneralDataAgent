import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_metric_analysis import actor, build_runtime, query

from generaldataagent.business.governance import Capability, CapabilityRegistry
from generaldataagent.business.optimization import (
    GovernedModelExecutor,
    ModelRoute,
    ProviderResult,
    ScopedResponseCache,
)
from generaldataagent.llm.cc_switch import LlmInvocationError, Narrative
from generaldataagent.llm.routed import LangChainRoutedProvider, NarrationSettings, RoutedNarrator
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.policy.budgets import TaskBudgetLedger

HASH = "sha256:" + "b" * 64


def routed_runtime(tmp_path):
    base = build_runtime(tmp_path)
    identity = actor()
    registry = CapabilityRegistry(tmp_path / "admissions", clock=lambda: datetime.now(UTC))
    spec = Capability(
        name="narrator",
        version="v1",
        kind="model",
        content_hash=HASH,
        owner="operator",
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        data_domains=identity.data_domains,
        regression_evidence_hash=HASH,
        security_evidence_hash=HASH,
        rollback_version="disabled",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    registry.propose(spec)
    for reviewer, discipline in (("q", "quality"), ("s", "security")):
        registry.review(spec.name, spec.version, reviewer_id=reviewer, discipline=discipline)
    registry.activate(spec.name, spec.version)
    budgets = TaskBudgetLedger(tmp_path / "budget")
    calls = []

    async def provider(request):
        evidence = json.loads(request.prompt)
        calls.append(evidence)
        return ProviderResult(
            text=Narrative(summary=f"Returned {evidence['row_count']} segments.").model_dump_json(),
            input_tokens=100,
            output_tokens=20,
        )

    route = ModelRoute(
        "narrator",
        "v1",
        HASH,
        frozenset({"narrative"}),
        frozenset({"internal"}),
        1,
        1,
        10,
        enabled=True,
    )
    executor = GovernedModelExecutor(
        routes=(route,),
        registry=registry,
        budgets=budgets,
        providers={(route.name, route.version): provider},
        authorized=lambda a, r: a == identity,
        cache=ScopedResponseCache(
            tmp_path / "cache", key=Fernet.generate_key(), clock=lambda: datetime.now(UTC)
        ),
    )
    narrator = RoutedNarrator(
        executor=executor,
        settings=NarrationSettings(prompt_version="v1", policy_version="v1"),
        authorization_version=lambda a: "acl-v1",
    )
    runtime = MetricAnalysisRuntime(
        catalog=base.catalog,
        executor=base.executor,
        artifacts=base.artifacts,
        audit=base.audit,
        budgets=budgets,
        routed_narrator=narrator,
    )
    return runtime, calls, registry


def test_routed_narration_main_workflow_cache_refresh_and_usage(tmp_path):
    runtime, calls, _ = routed_runtime(tmp_path)
    first = runtime.run(query(), actor())
    second = runtime.run(query(), actor())
    assert first["status"] == second["status"] == "completed"
    assert first["delivery_package"]["narrative_model"] == "narrator@v1"
    assert first["analysis_result"]["model_usage"]["cached"] is False
    assert second["analysis_result"]["model_usage"]["cached"] is True
    assert second["analysis_result"]["model_usage"]["input_tokens"] == 0
    assert len(calls) == 1
    # Query execution remains fresh on cache hits; changing the source invalidates the narrative.
    with sqlite3.connect(runtime.executor.database_path) as db:
        db.execute("INSERT INTO orders VALUES ('2026-10-04T00:00:00+00:00','partner')")
    third = runtime.run(query(), actor())
    assert third["analysis_result"]["model_usage"]["cached"] is False
    assert third["delivery_package"]["row_count"] == 3
    assert len(calls) == 2
    artifacts = third["delivery_package"]["evidence"]
    assert artifacts[-1]["lineage"] == [artifacts[0]["artifact_id"]]
    events = [json.loads(line) for line in runtime.audit.path.read_text().splitlines()]
    usage_events = [e for e in events if e["event_type"] == "analysis.model_usage"]
    assert len(usage_events) == 3
    assert all("rows" not in e["payload"] for e in usage_events)


def test_revoked_model_and_current_authorization_block_cached_narration(tmp_path):
    runtime, calls, registry = routed_runtime(tmp_path)
    runtime.run(query(), actor())
    runtime.routed_narrator.executor.authorized = lambda a, r: False
    with pytest.raises(PermissionError, match="access"):
        runtime.run(query(), actor())
    runtime.routed_narrator.executor.authorized = lambda a, r: a == actor()
    registry.revoke("narrator", "v1")
    with pytest.raises(PermissionError, match="admitted"):
        runtime.run(query(), actor())
    assert len(calls) == 1


def test_routed_analysis_requires_shared_budget_and_versions_invalidate_cache(tmp_path):
    runtime, calls, _ = routed_runtime(tmp_path)
    with pytest.raises(ValueError, match="share"):
        MetricAnalysisRuntime(
            catalog=runtime.catalog,
            executor=runtime.executor,
            artifacts=runtime.artifacts,
            routed_narrator=runtime.routed_narrator,
        )
    runtime.run(query(), actor())
    runtime.routed_narrator.authorization_version = lambda a: "acl-v2"
    runtime.run(query(), actor())
    assert len(calls) == 2
    runtime.routed_narrator.settings = runtime.routed_narrator.settings.model_copy(
        update={"max_cost_microunits": 0}
    )
    with pytest.raises(PermissionError):
        runtime.run(query(), actor())


def test_langchain_provider_preserves_usage_and_rejects_missing_usage():
    import asyncio

    class Model:
        def with_structured_output(self, schema, *, include_raw):
            assert schema is Narrative and include_raw is True
            return self

        async def ainvoke(self, messages, **kwargs):
            assert kwargs["max_tokens"] == 1024
            return {
                "parsed": Narrative(summary="Evidence-bound prose"),
                "parsing_error": None,
                "raw": SimpleNamespace(usage_metadata=self.usage),
            }

    model = Model()
    model.usage = {"input_tokens": 123, "output_tokens": 20}
    bridge = LangChainRoutedProvider(model)
    from test_stage5_optimization import request

    req = request(output_token_bound=1024)
    result = asyncio.run(bridge.invoke(req))
    assert result.input_tokens == 123
    model.usage = None
    with pytest.raises(LlmInvocationError):
        asyncio.run(bridge.invoke(req))
