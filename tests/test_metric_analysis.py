import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import ActorContext, MetricQuery, NaturalLanguageMetricQuery
from generaldataagent.llm import Narrative
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def build_runtime(tmp_path) -> MetricAnalysisRuntime:
    database = tmp_path / "orders.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (created_at TEXT, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [
                ("2026-10-01T00:00:00+00:00", "web"),
                ("2026-10-02T00:00:00+00:00", "web"),
                ("2026-10-03T00:00:00+00:00", "store"),
                ("2026-11-01T00:00:00+00:00", "web"),
            ],
        )
    metric = MetricDefinition(
        name="order_count",
        version="v1",
        description="Number of accepted orders",
        aliases=("order count", "number of orders", "orders", "订单量"),
        data_domain="commerce",
        relation="orders",
        expression="COUNT(*)",
        time_column="created_at",
        allowed_dimensions=("channel",),
        owner="commerce-data",
    )
    return MetricAnalysisRuntime(
        catalog=InMemoryMetricCatalog([metric]),
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        audit=JsonlAuditSink(tmp_path / "audit" / "events.jsonl"),
    )


class FakeNarrator:
    def generate(self, evidence) -> Narrative:
        return Narrative(
            summary=f"The query returned {evidence['row_count']} segments.",
            insights=("Web has more orders than store.",),
            limitations=("This is descriptive only.",),
            model="fake-model",
        )


def query() -> MetricQuery:
    return MetricQuery(
        metric_name="order_count",
        metric_version="v1",
        dimensions=("channel",),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        end_time=datetime(2026, 11, 1, tzinfo=UTC),
    )


def actor() -> ActorContext:
    return ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )


def test_metric_analysis_runs_end_to_end(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    result = runtime.run(query(), actor(), thread_id="metric-e2e")
    assert result["status"] == "completed"
    assert result["validation_result"]["status"] == "pass"
    assert result["delivery_package"]["rows"] == [["store", 1], ["web", 2]]
    assert result["delivery_package"]["table"]["columns"] == ["channel", "metric_value"]
    assert result["delivery_package"]["chart"]["chart_type"] == "bar"
    assert result["delivery_package"]["chart"]["x_field"] == "channel"
    assert result["delivery_package"]["claims"][0]["evidence_uris"]
    evidence = result["delivery_package"]["evidence"][0]
    assert evidence["uri"].startswith("artifact://local/")

    audit_lines = (tmp_path / "audit" / "events.jsonl").read_text().splitlines()
    assert {json.loads(line)["event_type"] for line in audit_lines} == {
        "analysis.started",
        "analysis.finished",
        "policy.decision",
    }


def test_metric_analysis_preserves_model_narrative_as_evidence(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.narrator = FakeNarrator()
    result = runtime.run(query(), actor(), thread_id="metric-narrative")

    delivery = result["delivery_package"]
    assert delivery["narrative_model"] == "fake-model"
    assert delivery["summary"] == "The query returned 2 segments."
    assert len(delivery["evidence"]) == 3
    assert {claim["claim_type"] for claim in delivery["claims"]} == {
        "interpretation",
        "limitation",
    }
    assert all(claim["evidence_uris"] for claim in delivery["claims"])


def test_metric_analysis_uses_kpi_chart_without_dimensions(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    aggregate_query = query().model_copy(update={"dimensions": ()})
    result = runtime.run(aggregate_query, actor(), thread_id="metric-kpi")

    delivery = result["delivery_package"]
    assert delivery["chart"]["chart_type"] == "kpi"
    assert delivery["chart"]["x_field"] is None
    assert delivery["table"]["rows"] == [[3]]


def test_claim_grounding_rejects_unknown_artifact(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    analysis = {
        "artifact": {"uri": "artifact://local/known"},
        "claims": [
            {
                "claim_id": "bad-claim",
                "claim_type": "fact",
                "text": "Unsupported fact",
                "evidence_uris": ["artifact://local/unknown"],
            }
        ],
    }
    assert runtime._claims_are_grounded(analysis) is False


def test_metric_analysis_rejects_uncertified_dimension(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    unsafe_query = query().model_copy(update={"dimensions": ("customer_email",)})
    with pytest.raises(ValueError, match="not certified"):
        runtime.run(unsafe_query, actor())


def test_natural_language_metric_question_resolves_and_runs(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    result = runtime.analyze_question(
        NaturalLanguageMetricQuery(
            question="Show the number of orders by channel",
            dimensions=("channel",),
            start_time=datetime(2026, 10, 1, tzinfo=UTC),
            end_time=datetime(2026, 11, 1, tzinfo=UTC),
        ),
        actor(),
        thread_id="natural-language-resolved",
    )

    assert result["status"] == "completed"
    assert result["resolution"]["status"] == "resolved"
    assert result["resolution"]["candidates"][0]["metric_name"] == "order_count"


def test_natural_language_metric_question_requires_clarification_on_ambiguity(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    order_metric = runtime.catalog.get("order_count", "v1")
    runtime.catalog = InMemoryMetricCatalog(
        [
            order_metric,
            order_metric.model_copy(
                update={
                    "name": "average_order_value",
                    "description": "Average accepted order value",
                    "aliases": ("average order value", "orders"),
                    "expression": "AVG(1)",
                    "unit": "USD",
                }
            ),
        ]
    )
    result = runtime.analyze_question(
        NaturalLanguageMetricQuery(question="orders"),
        actor(),
        thread_id="natural-language-ambiguous",
    )

    assert result["status"] == "needs_clarification"
    assert result["resolution"]["status"] == "ambiguous"
    assert len(result["resolution"]["candidates"]) == 2


def test_natural_language_resolution_hides_unauthorized_catalog_entries(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    result = runtime.analyze_question(
        NaturalLanguageMetricQuery(question="orders"),
        actor().model_copy(update={"data_domains": ("finance",)}),
        thread_id="natural-language-unauthorized",
    )

    assert result["status"] == "needs_clarification"
    assert result["resolution"]["status"] == "not_found"
    assert result["resolution"]["candidates"] == []


def test_metric_analysis_rejects_unauthorized_data_domain(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    unauthorized_actor = actor().model_copy(update={"data_domains": ("finance",)})
    with pytest.raises(PermissionError, match="not authorized"):
        runtime.run(query(), unauthorized_actor)

    audit_lines = (tmp_path / "audit" / "events.jsonl").read_text().splitlines()
    assert json.loads(audit_lines[-1])["event_type"] == "analysis.failed"


def test_metric_analysis_api(tmp_path) -> None:
    client = TestClient(create_app(build_runtime(tmp_path)))
    response = client.post(
        "/v1/analysis/metrics",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "commerce",
        },
        json=query().model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["delivery"]["row_count"] == 2


def test_metric_analysis_api_maps_authorization_failure(tmp_path) -> None:
    client = TestClient(create_app(build_runtime(tmp_path)))
    response = client.post(
        "/v1/analysis/metrics",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "finance",
        },
        json=query().model_dump(mode="json"),
    )
    assert response.status_code == 403


def test_metric_analysis_api_maps_unknown_metric(tmp_path) -> None:
    client = TestClient(create_app(build_runtime(tmp_path)))
    payload = query().model_dump(mode="json")
    payload["metric_name"] = "unknown_metric"
    response = client.post(
        "/v1/analysis/metrics",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "commerce",
        },
        json=payload,
    )
    assert response.status_code == 404


def test_natural_language_metric_api_returns_resolution(tmp_path) -> None:
    client = TestClient(create_app(build_runtime(tmp_path)))
    response = client.post(
        "/v1/analysis/questions",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "commerce",
        },
        json={
            "question": "Show the number of orders by channel",
            "dimensions": ["channel"],
            "start_time": "2026-10-01T00:00:00Z",
            "end_time": "2026-11-01T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["resolution"]["status"] == "resolved"
