import json

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition


def client(service):
    return TestClient(
        create_app(
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
        )
    )


def question_body(**overrides):
    return {
        "question": "订单量",
        "dimensions": ["channel"],
        "start_time": "2026-10-01T00:00:00+00:00",
        "end_time": "2026-11-01T00:00:00+00:00",
    } | overrides


def assert_no_question_text(path, question: str) -> None:
    raw = path.read_bytes()
    assert question.encode() not in raw
    assert json.dumps(question, ensure_ascii=False).encode() not in raw


def test_question_task_accepts_resolved_query_and_never_persists_question_text(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        token, headers = credentials(service.broker)
        request_headers = {**headers, "idempotency-key": "question-1"}
        api = client(service)

        accepted = api.post(
            "/v1/analysis/tasks/questions", headers=request_headers, json=question_body()
        )
        assert accepted.status_code == 202
        body = accepted.json()
        assert body["schema_version"] == "1.0"
        assert body["metric"] == {
            "schema_version": "1.0",
            "metric_name": "order_count",
            "metric_version": "v1",
            "description": "Number of accepted orders",
            "unit": "count",
            "matched_alias": "订单量",
        }
        assert body["task"]["status"] == "queued"
        assert body["task"]["attempts"] == 0

        repeated = api.post(
            "/v1/analysis/tasks/questions", headers=request_headers, json=question_body()
        )
        assert repeated.status_code == 202
        assert repeated.json() == body

        assert_no_question_text(tmp_path / "jobs.db", "订单量")
        assert b"order count" not in (tmp_path / "jobs.db").read_bytes()
        assert token.encode() not in (tmp_path / "jobs.db").read_bytes()

        completed = service.run_once()
        assert completed.status == "completed"
        result = api.get(f"/v1/analysis/tasks/{body['task']['task_id']}/result", headers=headers)
        assert result.status_code == 200
        delivery = result.json()["delivery"]
        assert delivery["metric"] == "order_count@v1"
        assert delivery["rows"] == [["store", 1], ["web", 2]]


def test_same_question_resolving_to_same_query_reuses_idempotent_task(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        token, headers = credentials(service.broker)
        request_headers = {**headers, "idempotency-key": "question-same-query"}
        api = client(service)
        first = api.post(
            "/v1/analysis/tasks/questions",
            headers=request_headers,
            json=question_body(question="订单量"),
        )
        second = api.post(
            "/v1/analysis/tasks/questions",
            headers=request_headers,
            json=question_body(question="orders"),
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["task"] == second.json()["task"]


def test_same_idempotency_key_with_different_resolved_query_is_a_conflict(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        token, headers = credentials(service.broker)
        request_headers = {**headers, "idempotency-key": "question-conflict"}
        api = client(service)
        assert (
            api.post(
                "/v1/analysis/tasks/questions", headers=request_headers, json=question_body()
            ).status_code
            == 202
        )
        changed = api.post(
            "/v1/analysis/tasks/questions",
            headers=request_headers,
            json=question_body(start_time=None, end_time=None),
        )
        assert changed.status_code == 409
        assert changed.json()["detail"] == "task_idempotency_request_changed"


def test_ambiguous_question_is_explicit_and_creates_no_task(tmp_path):
    original = build_runtime(tmp_path)
    base = original.catalog.get("order_count", "v1")
    catalog = InMemoryMetricCatalog(
        [
            base,
            MetricDefinition(
                name="order_total",
                version="v1",
                description="Total accepted orders",
                aliases=("订单量",),
                data_domain="commerce",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                owner="commerce-data",
            ),
        ]
    )
    with assembly(tmp_path, original, question_catalog=catalog) as (service, _):
        token, headers = credentials(service.broker)
        api = client(service)
        rejected = api.post(
            "/v1/analysis/tasks/questions",
            headers={**headers, "idempotency-key": "question-ambiguous"},
            json=question_body(),
        )
        assert rejected.status_code == 409
        body = rejected.json()
        assert body["status"] == "ambiguous"
        assert {item["metric_name"] for item in body["candidates"]} == {
            "order_count",
            "order_total",
        }
        assert "question" not in body
        assert all("question" not in item for item in body["candidates"])
        assert service.list_tasks(actor(), token).tasks == ()


def test_unmatched_question_is_explicit_and_creates_no_task(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        token, headers = credentials(service.broker)
        api = client(service)
        rejected = api.post(
            "/v1/analysis/tasks/questions",
            headers={**headers, "idempotency-key": "question-missing"},
            json=question_body(question="毛利率", dimensions=[]),
        )
        assert rejected.status_code == 409
        body = rejected.json()
        assert body["status"] == "not_found"
        assert body["candidates"] == []
        assert "毛利率" not in json.dumps(body, ensure_ascii=False)
        assert service.list_tasks(actor(), token).tasks == ()


def test_question_rejects_uncertified_dimension_before_acceptance(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        token, headers = credentials(service.broker)
        api = client(service)
        rejected = api.post(
            "/v1/analysis/tasks/questions",
            headers={**headers, "idempotency-key": "question-dimension"},
            json=question_body(dimensions=["created_at"]),
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "invalid_question_submission"
        assert service.list_tasks(actor(), token).tasks == ()


def test_question_acceptance_requires_catalog_and_analysis_permission(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        api = client(service)
        unavailable = api.post(
            "/v1/analysis/tasks/questions",
            headers={**headers, "idempotency-key": "question-unavailable"},
            json=question_body(),
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "question_acceptance_not_configured"

    with assembly(tmp_path / "authorized", original, question_catalog=original.catalog) as (
        service,
        _,
    ):
        _, denied_headers = credentials(service.broker, permissions=frozenset())
        api = client(service)
        denied = api.post(
            "/v1/analysis/tasks/questions",
            headers={**denied_headers, "idempotency-key": "question-denied"},
            json=question_body(),
        )
        assert denied.status_code == 403
        assert service.queue.list_tasks(actor()).tasks == ()


def test_api_rejects_question_catalog_that_disagrees_with_runtime(tmp_path):
    original = build_runtime(tmp_path)
    (tmp_path / "other").mkdir()
    other = build_runtime(tmp_path / "other")
    with (
        assembly(tmp_path, original, question_catalog=original.catalog) as (service, _),
        pytest.raises(ValueError, match="metric_tasks_must_share_runtime_catalog"),
    ):
        create_app(
            metric_runtime=other,
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
        )
