import asyncio
import sqlite3

import pytest

from generaldataagent.agents import MetricSupervisorRuntime
from generaldataagent.agents.metric_supervisor import _candidate_passed
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def build_supervisor_runtime(tmp_path) -> MetricSupervisorRuntime:
    database = tmp_path / "metrics.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts (occurred_at TEXT, segment TEXT)")
        connection.executemany(
            "INSERT INTO facts VALUES (?, ?)",
            [("2025-01-01T00:00:00+00:00", "a"), ("2025-01-02T00:00:00+00:00", "b")],
        )
    metric = MetricDefinition(
        name="event_count",
        version="v1",
        description="Number of events",
        data_domain="test.events",
        relation="facts",
        expression="COUNT(*)",
        time_column="occurred_at",
        allowed_dimensions=("segment",),
        owner="test",
    )
    catalog = InMemoryMetricCatalog([metric])
    runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    return MetricSupervisorRuntime(model=None, catalog=catalog, metric_runtime=runtime)


def actor(domains=("test.events",)) -> ActorContext:
    return ActorContext(
        user_id="user",
        tenant_id="tenant",
        project_id="project",
        data_domains=domains,
    )


def test_actor_bound_tools_enforce_order_and_validation(tmp_path) -> None:
    supervisor = build_supervisor_runtime(tmp_path)
    candidates = {}
    events = []
    groups = supervisor._build_actor_tools(actor(), candidates, events)

    discovery = groups["discovery"][0].invoke(
        {"metric_name": "event_count", "metric_version": "v1", "dimensions": ["segment"]}
    )
    assert discovery["qualified_metric"] == "event_count@v1"
    assert discovery["certification_status"] == "certified"
    assert discovery["expression"] == "COUNT(*)"
    assert discovery["owner"] == "test"

    analysis = groups["data_analysis"][0].invoke(
        {
            "metric_name": "event_count",
            "metric_version": "v1",
            "dimensions": ["segment"],
        }
    )
    assert analysis["validation_status"] == "pass"

    repeated = groups["data_analysis"][0].invoke(
        {
            "metric_name": "event_count",
            "metric_version": "v1",
            "dimensions": ["segment"],
        }
    )
    assert repeated["task_id"] == analysis["task_id"]

    verification = groups["verification"][0].invoke({})
    assert verification["status"] == "pass"
    delivery = groups["reporting"][0].invoke({})
    assert delivery["rows"] == [["a", 1], ["b", 1]]
    assert events == ["discovery", "data-analyst", "verifier", "reporter"]


def test_actor_bound_discovery_rejects_unauthorized_domain(tmp_path) -> None:
    supervisor = build_supervisor_runtime(tmp_path)
    groups = supervisor._build_actor_tools(actor(("other",)), {})
    with pytest.raises(PermissionError, match="unauthorized"):
        groups["discovery"][0].invoke(
            {"metric_name": "event_count", "metric_version": "v1", "dimensions": []}
        )


def test_verifier_cannot_read_unknown_candidate(tmp_path) -> None:
    supervisor = build_supervisor_runtime(tmp_path)
    groups = supervisor._build_actor_tools(actor(), {})
    with pytest.raises(PermissionError, match="expected 'discovery'"):
        groups["verification"][0].invoke({})


def test_actor_bound_tools_reject_out_of_order_analysis(tmp_path) -> None:
    supervisor = build_supervisor_runtime(tmp_path)
    groups = supervisor._build_actor_tools(actor(), {})
    with pytest.raises(PermissionError, match="expected 'discovery'"):
        groups["data_analysis"][0].invoke(
            {"metric_name": "event_count", "metric_version": "v1", "dimensions": []}
        )


def test_candidate_passed_fails_closed_for_missing_state() -> None:
    assert _candidate_passed({}) is False


def test_supervisor_fails_closed_when_model_provider_errors(monkeypatch, tmp_path) -> None:
    runtime = build_supervisor_runtime(tmp_path)

    class FailingSupervisor:
        async def ainvoke(self, state, config):
            raise TimeoutError("provider timeout")

    monkeypatch.setattr(
        "generaldataagent.agents.metric_supervisor.build_supervisor",
        lambda **kwargs: FailingSupervisor(),
    )
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1"),
        actor(),
        thread_id="provider-timeout-test",
    )

    assert result.status == "failed"
    assert result.delegated_roles == ()
    assert result.limitations == ("model invocation failed: TimeoutError",)


def test_supervisor_fails_closed_when_structured_response_is_missing(monkeypatch, tmp_path) -> None:
    runtime = build_supervisor_runtime(tmp_path)

    class MissingResponseSupervisor:
        async def ainvoke(self, state, config):
            return {"structured_response": None}

    monkeypatch.setattr(
        "generaldataagent.agents.metric_supervisor.build_supervisor",
        lambda **kwargs: MissingResponseSupervisor(),
    )
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1"),
        actor(),
        thread_id="missing-response-test",
    )

    assert result.status == "failed"
    assert result.delegated_roles == ()
    assert result.limitations == ("missing structured Supervisor response",)


def test_supervisor_uses_fixed_reporter_after_verified_candidate(monkeypatch, tmp_path) -> None:
    runtime = build_supervisor_runtime(tmp_path)

    class FakeSupervisor:
        calls = 0

        def __init__(self, groups) -> None:
            self.groups = groups

        async def ainvoke(self, state, config):
            self.calls += 1
            arguments = {
                "metric_name": "event_count",
                "metric_version": "v1",
                "dimensions": ["segment"],
            }
            self.groups["discovery"][0].invoke(arguments)
            self.groups["data_analysis"][0].invoke(arguments)
            self.groups["verification"][0].invoke({})
            return {
                "structured_response": None,
            }

    built = []

    def fake_builder(**kwargs):
        assert "MUST delegate" in kwargs["system_prompt"]
        supervisor = FakeSupervisor(kwargs["tool_groups"])
        built.append(supervisor)
        return supervisor

    monkeypatch.setattr(
        "generaldataagent.agents.metric_supervisor.build_supervisor",
        fake_builder,
    )
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1", dimensions=("segment",)),
        actor(),
        thread_id="repair-test",
    )

    assert built[0].calls == 1
    assert result.status == "completed"
    assert result.summary == "Certified metric query returned 2 row(s)."
    assert result.delegated_roles == ("discovery", "data-analyst", "verifier", "reporter")
    assert result.artifact_uris


def test_supervisor_enforces_total_runtime_budget(monkeypatch, tmp_path) -> None:
    runtime = build_supervisor_runtime(tmp_path)

    class SlowSupervisor:
        async def ainvoke(self, state, config):
            await asyncio.sleep(1)
            return {"structured_response": None}

    monkeypatch.setattr(
        "generaldataagent.agents.metric_supervisor.build_supervisor",
        lambda **kwargs: SlowSupervisor(),
    )
    result = runtime.run(
        MetricQuery(metric_name="event_count", metric_version="v1"),
        actor(),
        thread_id="total-timeout-test",
        max_runtime_seconds=0.01,
    )

    assert result.status == "failed"
    assert result.limitations == ("model invocation failed: TimeoutError",)
