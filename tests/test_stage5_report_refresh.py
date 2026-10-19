import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_metric_analysis import actor, build_runtime, query
from test_stage5_actions import HASH

from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.business.report_refresh import MetricReportRefresher, ReportGenerationBusy
from generaldataagent.business.schedules import ReportSchedule, ReportScheduler
from generaldataagent.contracts import MetricQuery
from generaldataagent.contracts.business import ReportDraft
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker


def setup_refresh(tmp_path):
    analysis = build_runtime(tmp_path)
    refresher = MetricReportRefresher(tmp_path / "reports", analysis=analysis)
    broker = SessionBroker()
    approvals = SQLiteApprovalStore(tmp_path / "approvals")
    identity = actor()
    token = broker.issue(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        data_domains=frozenset(identity.data_domains),
        permissions=frozenset({"business:read", "business:propose", "actions:resume"}),
    )
    actions = BusinessActionRuntime(
        tmp_path / "actions",
        broker=broker,
        approvals=approvals,
        controls=(
            ActionControl(
                "report",
                "report",
                "owner",
                identity.tenant_id,
                identity.project_id,
                frozenset(identity.data_domains),
                enabled=True,
            ),
        ),
        artifact_reader=refresher.read_artifact,
    )
    scheduler = ReportScheduler(tmp_path / "schedules", actions=actions, refresher=refresher)
    spec = ReportSchedule(
        schedule_id=uuid4(),
        destination="report",
        report=ReportDraft(title="Orders report", report_hash=HASH, period="template"),
        starts_at=datetime.now(UTC) + timedelta(minutes=1),
        interval_minutes=60,
        occurrences=3,
        query=query(),
    )
    scheduler.register(spec, token)
    return scheduler, spec, token, refresher, approvals


def test_reports_refresh_source_freeze_artifact_and_require_own_approval(tmp_path):
    scheduler, spec, token, refresher, approvals = setup_refresh(tmp_path)
    scheduler.actions.clock = lambda: spec.starts_at + timedelta(seconds=1)
    first = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    first_payload = first["intent"]["payload"]
    assert first_payload["report_hash"] != HASH
    artifact = first_payload["report_artifact"]
    from generaldataagent.contracts import ArtifactRef

    raw = refresher.read_artifact(ArtifactRef.model_validate(artifact), actor())
    assert json.loads(raw)["delivery"]["rows"] == [["store", 1], ["web", 2]]
    assert scheduler.tick(spec.schedule_id, token)["drafts"] == []
    with pytest.raises(PermissionError):
        scheduler.actions.execute(UUID(first["intent"]["action_id"]), token)
    approvals.approve(UUID(first["approval_id"]), approver_id="actual-test-reviewer")
    scheduler.actions.execute(UUID(first["intent"]["action_id"]), token)
    with sqlite3.connect(refresher.analysis.executor.database_path) as db:
        db.execute("INSERT INTO orders VALUES ('2026-10-04T00:00:00+00:00','partner')")
    scheduler.actions.clock = lambda: spec.starts_at + timedelta(minutes=61)
    second = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    assert second["intent"]["payload"]["report_hash"] != first_payload["report_hash"]
    assert second["approval_id"] != first["approval_id"]
    assert (
        len(
            json.loads(
                refresher.read_artifact(
                    ArtifactRef.model_validate(second["intent"]["payload"]["report_artifact"]),
                    actor(),
                )
            )["delivery"]["rows"]
        )
        == 3
    )


def test_freeze_survives_proposal_failure_and_source_change(tmp_path, monkeypatch):
    scheduler, spec, token, refresher, _ = setup_refresh(tmp_path)
    scheduler.actions.clock = lambda: spec.starts_at + timedelta(seconds=1)
    propose = scheduler.actions.propose
    monkeypatch.setattr(
        scheduler.actions, "propose", lambda *a: (_ for _ in ()).throw(ConnectionError("offline"))
    )
    with pytest.raises(ConnectionError):
        scheduler.tick(spec.schedule_id, token)
    ref = refresher.prepare(schedule_id=spec.schedule_id, index=0, query=spec.query, actor=actor())
    with sqlite3.connect(refresher.analysis.executor.database_path) as db:
        db.execute("DELETE FROM orders")
    monkeypatch.setattr(scheduler.actions, "propose", propose)
    result = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    assert result["intent"]["payload"]["report_hash"] == ref.content_hash
    assert result["intent"]["payload"]["report_artifact"]["artifact_id"] == str(ref.artifact_id)


def test_cancel_during_refresh_and_in_progress_generation(tmp_path, monkeypatch):
    scheduler, spec, token, refresher, _ = setup_refresh(tmp_path)
    scheduler.actions.clock = lambda: spec.starts_at + timedelta(seconds=1)
    prepare = refresher.prepare

    def cancel_then_prepare(**kwargs):
        # Cancellation must not be blocked by an open schedule transaction during model/query work.
        scheduler.cancel(spec.schedule_id, token)
        return prepare(**kwargs)

    monkeypatch.setattr(refresher, "prepare", cancel_then_prepare)
    with pytest.raises(PermissionError, match="disabled"):
        scheduler.tick(spec.schedule_id, token)
    with scheduler.actions._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_actions").fetchone()[0] == 0
    from uuid import uuid5

    other = uuid4()
    from generaldataagent.business.governance import actor_scope

    with sqlite3.connect(refresher.path) as db:
        db.execute(
            "INSERT INTO refreshed_reports VALUES(?,?,?,'generating',NULL)",
            (str(uuid5(other, "report:0")), actor_scope(actor()), query().model_dump_json()),
        )
    with pytest.raises(ReportGenerationBusy):
        prepare(schedule_id=other, index=0, query=query(), actor=actor())


def test_rolling_window_and_tamper_block_before_action(tmp_path):
    scheduler, spec, token, refresher, approvals = setup_refresh(tmp_path)
    rolling = spec.model_copy(
        update={
            "schedule_id": uuid4(),
            "query": MetricQuery(
                metric_name="order_count", metric_version="v1", dimensions=("channel",)
            ),
            "lookback_minutes": 60,
        }
    )
    scheduler.register(rolling, token)
    scheduler.actions.clock = lambda: rolling.starts_at + timedelta(seconds=1)
    draft = scheduler.tick(rolling.schedule_id, token)["drafts"][0]
    from generaldataagent.contracts import ArtifactRef

    ref = ArtifactRef.model_validate(draft["intent"]["payload"]["report_artifact"])
    content = json.loads(refresher.read_artifact(ref, actor()))
    assert (
        datetime.fromisoformat(content["query"]["end_time"].replace("Z", "+00:00"))
        == rolling.starts_at
    )
    approvals.approve(UUID(draft["approval_id"]), approver_id="reviewer")
    path = refresher.analysis.artifacts.root / ref.uri.removeprefix("artifact://local/")
    path.write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        scheduler.actions.execute(UUID(draft["intent"]["action_id"]), token)
    with pytest.raises(PermissionError):
        refresher.read_artifact(ref, actor().model_copy(update={"user_id": "other"}))
