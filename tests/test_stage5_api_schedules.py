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


def recovering_schedule(tmp_path, runtime, token):
    spec = ReportSchedule(
        schedule_id=uuid4(),
        destination="report",
        report=PAYLOADS[-1],
        starts_at=datetime.now(UTC) + timedelta(minutes=1),
        interval_minutes=60,
        occurrences=3,
        revision_policy="current_at_proposal",
    )
    scheduler = ReportScheduler(tmp_path / "schedules.db", actions=runtime)
    scheduler.register(spec, token)
    return scheduler, spec


def test_report_recovers_after_expired_or_cancelled_occurrence(setup, tmp_path):
    runtime, approvals, _, token = setup
    scheduler, spec = recovering_schedule(tmp_path, runtime, token)
    runtime.clock = lambda: spec.starts_at + timedelta(minutes=61)
    result = scheduler.tick(spec.schedule_id, token)
    assert result["expired"] == [0]
    second = result["drafts"][0]
    assert second["intent"]["expected_revision"] == 0
    runtime.cancel(UUID(second["intent"]["action_id"]), token)
    runtime.clock = lambda: spec.starts_at + timedelta(minutes=121)
    third = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    assert third["intent"]["expected_revision"] == 0
    approvals.approve(UUID(third["approval_id"]), approver_id="bob")
    assert runtime.execute(UUID(third["intent"]["action_id"]), token)["state"] == "completed"


def test_report_retry_keeps_original_revision_and_approval_after_lost_ack(
    setup, tmp_path, monkeypatch
):
    runtime, approvals, _, token = setup
    scheduler, spec = recovering_schedule(tmp_path, runtime, token)
    runtime.clock = lambda: spec.starts_at + timedelta(seconds=1)
    propose = runtime.propose
    original = []

    def lose_ack(intent, token):
        original.append(propose(intent, token))
        raise ConnectionError("lost scheduler acknowledgment")

    monkeypatch.setattr(runtime, "propose", lose_ack)
    with pytest.raises(ConnectionError):
        scheduler.tick(spec.schedule_id, token)
    first = original[0]
    approvals.approve(UUID(first["approval_id"]), approver_id="bob")
    runtime.execute(UUID(first["intent"]["action_id"]), token)
    monkeypatch.setattr(runtime, "propose", propose)
    restarted = ReportScheduler(scheduler.path, actions=runtime)
    replay = restarted.tick(spec.schedule_id, token)["drafts"][0]
    assert replay["approval_id"] == first["approval_id"]
    assert replay["intent"]["expected_revision"] == 0
    runtime.clock = lambda: spec.starts_at + timedelta(minutes=61)
    second = restarted.tick(spec.schedule_id, token)["drafts"][0]
    assert second["intent"]["expected_revision"] == 1
    assert second["approval_id"] != first["approval_id"]


def test_pending_approval_is_not_rebased_by_later_report(setup, tmp_path):
    runtime, approvals, _, token = setup
    scheduler, spec = recovering_schedule(tmp_path, runtime, token)
    runtime.clock = lambda: spec.starts_at + timedelta(seconds=1)
    first = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    runtime.clock = lambda: spec.starts_at + timedelta(minutes=61)
    second = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    # Both were proposed against the same current resource; only one may win execution.
    assert first["intent"]["expected_revision"] == second["intent"]["expected_revision"] == 0
    approvals.approve(UUID(second["approval_id"]), approver_id="bob")
    runtime.execute(UUID(second["intent"]["action_id"]), token)
    # The first draft has expired, and is never silently rebased or approved again.
    assert (
        runtime.get(UUID(first["intent"]["action_id"]), token)["intent"]["expected_revision"] == 0
    )


def test_resource_change_requires_new_approval_instead_of_rebasing(setup, tmp_path):
    runtime, approvals, _, token = setup
    scheduler, spec = recovering_schedule(tmp_path, runtime, token)
    runtime.clock = lambda: spec.starts_at + timedelta(seconds=1)
    pending = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    other = intent(PAYLOADS[-1])
    other_draft = runtime.propose(other, token)
    approvals.approve(UUID(other_draft["approval_id"]), approver_id="bob")
    runtime.execute(other.action_id, token)
    approvals.approve(UUID(pending["approval_id"]), approver_id="bob")
    with pytest.raises(ValueError, match="revision_changed"):
        runtime.execute(UUID(pending["intent"]["action_id"]), token)
    assert (
        runtime.get(UUID(pending["intent"]["action_id"]), token)["intent"]["expected_revision"] == 0
    )
