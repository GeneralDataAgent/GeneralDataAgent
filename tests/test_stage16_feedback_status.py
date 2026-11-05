"""Creator-private feedback eligibility/status; foreign tasks remain opaque."""

import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from generaldataagent.api.app import create_app
from generaldataagent.business.governance import (
    Feedback,
    FeedbackRegistry,
    FeedbackTaskStatus,
    SubmittedFeedback,
)
from generaldataagent.contracts import ActorContext
from generaldataagent.security.identity import SQLiteSessionBroker

OWNER = ActorContext(user_id="alice", tenant_id="t", project_id="p", data_domains=("sales",))
OTHER = ActorContext(user_id="bob", tenant_id="t", project_id="p", data_domains=("sales",))
DIGEST = "sha256:" + "a" * 64


def feedback(task_id, **overrides):
    values = {
        "task_id": task_id,
        "result_hash": DIGEST,
        "category": "correctness",
        "rating": 4,
        "redacted_question": "How many orders?",
        "expected_outcome": "Use the certified count",
    }
    return Feedback(**{**values, **overrides})


def registered(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback.sqlite")
    task_id = uuid4()
    registry.register_task(task_id, OWNER, DIGEST)
    return registry, task_id


def test_status_is_opaque_before_registration_and_for_foreign_scopes(tmp_path):
    registry, task_id = registered(tmp_path)
    assert registry.task_status(task_id, OTHER) == FeedbackTaskStatus(
        task_id=task_id, eligible=False
    )
    assert registry.task_status(uuid4(), OWNER).eligible is False


def test_status_exposes_only_bounded_feedback_facts(tmp_path):
    registry, task_id = registered(tmp_path)
    item = feedback(task_id)
    registry.submit(item, OWNER)
    status = registry.task_status(task_id, OWNER)
    assert status.eligible is True
    assert status.result_hash == DIGEST
    assert status.feedback == (
        SubmittedFeedback(
            feedback_id=item.feedback_id,
            category="correctness",
            rating=4,
            review_count=0,
            accepted_review_count=0,
        ),
    )
    assert status.has_more_feedback is False
    assert set(status.feedback[0].model_dump()) == {
        "schema_version",
        "feedback_id",
        "category",
        "rating",
        "review_count",
        "accepted_review_count",
    }
    registry.review(item.feedback_id, reviewer_id="q", accepted=True)
    registry.review(item.feedback_id, reviewer_id="s", accepted=False)
    reviewed = registry.task_status(task_id, OWNER).feedback[0]
    assert (reviewed.review_count, reviewed.accepted_review_count) == (2, 1)
    assert registry.task_status(task_id, OTHER).eligible is False


def test_status_is_bounded_to_fifty_entries(tmp_path):
    registry, task_id = registered(tmp_path)
    for _ in range(51):
        registry.submit(feedback(task_id), OWNER)
    status = registry.task_status(task_id, OWNER)
    assert len(status.feedback) == 50
    assert status.has_more_feedback is True


def test_status_fails_closed_on_tampered_feedback(tmp_path):
    registry, task_id = registered(tmp_path)
    registry.submit(feedback(task_id), OWNER)
    with sqlite3.connect(registry.path) as db, db:
        db.execute("UPDATE feedback SET body=replace(body,'certified count','other count')")
    with pytest.raises(PermissionError):
        registry.task_status(task_id, OWNER)


def test_status_contract_refuses_incoherent_states():
    with pytest.raises(ValidationError):
        FeedbackTaskStatus(task_id=uuid4(), eligible=True, result_hash=None)
    with pytest.raises(ValidationError):
        FeedbackTaskStatus(task_id=uuid4(), eligible=False, result_hash=DIGEST)
    with pytest.raises(ValidationError):
        FeedbackTaskStatus(
            task_id=uuid4(),
            eligible=False,
            feedback=(
                SubmittedFeedback(
                    feedback_id=uuid4(),
                    category="safety",
                    rating=1,
                    review_count=0,
                    accepted_review_count=0,
                ),
            ),
        )
    with pytest.raises(ValidationError):
        SubmittedFeedback(
            feedback_id=uuid4(),
            category="safety",
            rating=1,
            review_count=1,
            accepted_review_count=2,
        )


def api(tmp_path):
    registry, task_id = registered(tmp_path)
    broker = SQLiteSessionBroker(tmp_path / "sessions.db")
    token = broker.issue(
        user_id=OWNER.user_id,
        tenant_id=OWNER.tenant_id,
        project_id=OWNER.project_id,
        data_domains=frozenset(OWNER.data_domains),
        permissions=frozenset({"business:read"}),
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": OWNER.user_id,
        "x-tenant-id": OWNER.tenant_id,
        "x-project-id": OWNER.project_id,
        "x-data-domain": "sales",
    }
    client = TestClient(create_app(session_broker=broker, feedback_registry=registry))
    return registry, task_id, client, headers


def test_http_status_requires_identity_and_keeps_foreign_tasks_opaque(tmp_path):
    registry, task_id, client, headers = api(tmp_path)
    path = f"/v1/business/feedback/tasks/{task_id}"
    assert client.get(path, headers={}).status_code == 401
    wrong = {**headers, "x-user-id": "bob"}
    assert client.get(path, headers=wrong).status_code == 403
    unknown = client.get(f"/v1/business/feedback/tasks/{uuid4()}", headers=headers)
    assert unknown.status_code == 200
    assert unknown.json()["eligible"] is False
    assert "result_hash" in unknown.json() and unknown.json()["result_hash"] is None
    item = feedback(task_id)
    registry.submit(item, OWNER)
    body = client.get(path, headers=headers).json()
    assert body["schema_version"] == "1.0"
    assert body["eligible"] is True
    assert body["result_hash"] == DIGEST
    assert body["feedback"][0]["feedback_id"] == str(item.feedback_id)
    assert body["feedback"][0]["review_count"] == 0
    assert "expected_outcome" not in body["feedback"][0]
    assert "redacted_question" not in body["feedback"][0]


def test_status_fails_closed_when_the_store_is_unreadable(tmp_path):
    registry, task_id = registered(tmp_path)
    with sqlite3.connect(registry.path) as db, db:
        db.execute("DROP TABLE feedback_reviews")
    with pytest.raises(ValueError, match="feedback_status_unavailable"):
        registry.task_status(task_id, OWNER)
