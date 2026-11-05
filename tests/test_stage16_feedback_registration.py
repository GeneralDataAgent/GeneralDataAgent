"""Completed async tasks register the exact delivery hash for creator feedback."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime, query
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app
from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.orchestration.task_service import MetricTaskService


def test_worker_registers_a_completed_task_for_creator_feedback(tmp_path):
    original = build_runtime(tmp_path)
    registry = FeedbackRegistry(tmp_path / "feedback.db")
    with assembly(tmp_path, original, feedback=registry) as (service, _saver):
        token, headers = credentials(
            service.broker, permissions=frozenset({"analysis:run", "business:read"})
        )
        client = TestClient(
            create_app(
                session_broker=service.broker,
                task_events=service.events,
                metric_tasks=service,
                feedback_registry=registry,
            )
        )
        submitted = client.post(
            "/v1/analysis/tasks", headers=headers, json=query().model_dump(mode="json")
        )
        assert submitted.status_code == 202
        task_id = UUID(submitted.json()["task_id"])
        path = f"/v1/business/feedback/tasks/{task_id}"
        assert client.get(path, headers=headers).json()["eligible"] is False
        completed = service.run_once()
        assert completed is not None and completed.status == "completed"
        status = client.get(path, headers=headers).json()
        assert status["eligible"] is True
        assert status["result_hash"] is not None
        assert status["feedback"] == []
        result = service.result(task_id, actor(), token)
        assert status["result_hash"] == content_hash(result.delivery.model_dump(mode="json"))
        assert registry.registered(task_id, actor())


def test_app_refuses_a_feedback_registry_the_worker_cannot_reach(tmp_path):
    original = build_runtime(tmp_path)
    with (
        assembly(tmp_path, original) as (service, _saver),
        pytest.raises(ValueError, match="feedback_registry_must_share_task_service"),
    ):
        create_app(
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
            feedback_registry=FeedbackRegistry(tmp_path / "other.db"),
        )


def test_task_service_rejects_an_unqualified_feedback_port():
    with pytest.raises(ValueError, match="task_feedback_registry_required"):
        MetricTaskService(
            queue=None,
            broker=None,
            events=None,
            budgets=None,
            runtime_factory=None,
            feedback=object(),
        )
