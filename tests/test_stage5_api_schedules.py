from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_stage5_actions import PAYLOADS, intent
from test_stage5_actions import setup as setup

from generaldataagent.api.app import create_app
from generaldataagent.business.schedules import ReportSchedule, ReportScheduler


def headers(token):
    return {
        "authorization": "Bearer " + token,
        "x-user-id": "alice",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "sales",
    }


def test_authenticated_draft_api_and_no_approval_endpoint(setup):
    runtime, approvals, broker, token = setup
    app = TestClient(create_app(session_broker=broker, business_actions=runtime))
    action = intent()
    endpoint = "/v1/business/drafts"
    assert app.post(endpoint, json=action.model_dump(mode="json")).status_code == 401
    response = app.post(endpoint, json=action.model_dump(mode="json"), headers=headers(token))
    assert response.status_code == 200
    approval = response.json()["approval_id"]
    assert (
        app.post(f"{endpoint}/{action.action_id}/approve", headers=headers(token)).status_code
        == 404
    )
    assert (
        app.post(f"{endpoint}/{action.action_id}/execute", headers=headers(token)).status_code
        == 403
    )
    approvals.approve(UUID(approval), approver_id="bob")
    assert (
        app.post(f"{endpoint}/{action.action_id}/execute", headers=headers(token)).status_code
        == 200
    )
    assert (
        app.get(f"{endpoint}/{action.action_id}", headers=headers(token)).json()["state"]
        == "completed"
    )
    assert (
        app.get("/v1/capabilities", headers=headers(token)).json()["external_business_delivery"]
        is False
    )
    read_token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:read"}),
    )
    assert (
        app.post(
            endpoint, json=intent().model_dump(mode="json"), headers=headers(read_token)
        ).status_code
        == 403
    )
    with pytest.raises(ValueError, match="not qualified"):
        create_app(session_broker=broker, business_actions=runtime, production_mode=True)


def test_periodic_reports_one_draft_per_occurrence_and_independent_approval(setup, tmp_path):
    runtime, approvals, broker, token = setup
    starts = datetime.now(UTC) + timedelta(minutes=1)
    schedule = ReportSchedule(
        schedule_id=uuid4(),
        destination="report",
        report=PAYLOADS[-1],
        starts_at=starts,
        interval_minutes=60,
        occurrences=3,
    )
    scheduler = ReportScheduler(tmp_path / "schedules.db", actions=runtime)
    scheduler.register(schedule, token)
    assert scheduler.tick(schedule.schedule_id, token)["drafts"] == []
    runtime.clock = lambda: starts + timedelta(seconds=1)
    first = scheduler.tick(schedule.schedule_id, token)["drafts"][0]
    assert first["state"] == "pending"
    assert scheduler.tick(schedule.schedule_id, token)["drafts"] == []
    # Identity clock stays real; only scheduling time moves.
    approvals.approve(UUID(first["approval_id"]), approver_id="bob")
    runtime.execute(UUID(first["intent"]["action_id"]), token)
    runtime.clock = lambda: starts + timedelta(minutes=61)
    restarted = ReportScheduler(scheduler.path, actions=runtime)
    second = restarted.tick(schedule.schedule_id, token)["drafts"][0]
    assert second["approval_id"] != first["approval_id"]
    with pytest.raises(PermissionError):
        runtime.execute(UUID(second["intent"]["action_id"]), token)
    restarted.cancel(schedule.schedule_id, token)
    with pytest.raises(PermissionError, match="disabled"):
        restarted.tick(schedule.schedule_id, token)


def test_schedule_expired_occurrences_and_scope(setup, tmp_path):
    runtime, _, broker, token = setup
    starts = datetime.now(UTC) + timedelta(minutes=1)
    spec = ReportSchedule(
        schedule_id=uuid4(),
        destination="report",
        report=PAYLOADS[-1],
        starts_at=starts,
        interval_minutes=60,
        occurrences=3,
    )
    scheduler = ReportScheduler(tmp_path / "schedules.db", actions=runtime)
    scheduler.register(spec, token)
    with pytest.raises(ValueError):
        scheduler.register(spec, token)
    runtime.clock = lambda: starts + timedelta(days=1)
    result = scheduler.tick(spec.schedule_id, token)
    assert result["expired"] == [0, 1, 2]
    assert result["complete"] and not result["drafts"]
    foreign = broker.issue(
        user_id="eve",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose"}),
    )
    with pytest.raises(PermissionError):
        scheduler.tick(spec.schedule_id, foreign)


def test_feedback_api_requires_registered_result_and_scope(setup, tmp_path):
    from test_stage5_actions import HASH

    from generaldataagent.business.governance import Feedback, FeedbackRegistry
    from generaldataagent.contracts import ActorContext

    _, _, broker, token = setup
    registry = FeedbackRegistry(tmp_path / "feedback.db")
    app = TestClient(create_app(session_broker=broker, feedback_registry=registry))
    item = Feedback(
        task_id=uuid4(),
        result_hash=HASH,
        category="grounding",
        rating=3,
        redacted_question="Regional revenue",
        expected_outcome="Include citations",
    )
    path = "/v1/business/feedback"
    assert (
        app.post(path, json=item.model_dump(mode="json"), headers=headers(token)).status_code == 403
    )
    actor = ActorContext(user_id="alice", tenant_id="t", project_id="p", data_domains=("sales",))
    registry.register_task(item.task_id, actor, HASH)
    assert (
        app.post(path, json=item.model_dump(mode="json"), headers=headers(token)).json()["state"]
        == "awaiting_independent_review"
    )
    assert (
        app.post(path, json=item.model_dump(mode="json"), headers=headers(token)).status_code == 409
    )
    with pytest.raises(ValueError, match="sessions"):
        create_app(feedback_registry=registry)
