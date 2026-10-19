import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_metric_analysis import actor
from test_stage5_actions import intent
from test_stage5_feishu import FakeFeishu
from test_stage5_report_refresh import setup_refresh

from generaldataagent.business.feishu import FeishuReportBackend, FeishuTarget
from generaldataagent.business.worker import ReportWorker, ScheduleWorkload
from generaldataagent.contracts.business import ActionIntent, NotificationDraft
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.security.workload import (
    OIDCWorkloadSessionIssuer,
    ProjectedWorkloadToken,
    WorkloadSessionBroker,
)

ISSUER = "https://local-fixture.invalid"


def prepare(tmp_path, *, domains=("commerce",)):
    scheduler, spec, _, _, approvals = setup_refresh(tmp_path)
    from dataclasses import replace

    scheduler.actions.controls[("report", "report")] = replace(
        scheduler.actions.controls[("report", "report")], data_domains=frozenset(domains)
    )
    authority = {"active": True}
    worker_ref = {}
    broker = WorkloadSessionBroker(
        tmp_path / "workload-sessions",
        allowed=lambda w, s: (
            authority["active"]
            and s == "report-worker"
            and worker_ref["worker"].authorizes_workload(w)
        ),
        action_allowed=lambda w, s, a, c, f: worker_ref["worker"].authorizes_action(w, a, c, f),
    )
    scheduler.actions.broker = broker
    scheduler.actions.clock = lambda: spec.starts_at + timedelta(seconds=1)
    owner = broker.issue(
        user_id=actor().user_id,
        tenant_id=actor().tenant_id,
        project_id=actor().project_id,
        data_domains=frozenset(domains),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credential = [sign(key)]
    issuer = OIDCWorkloadSessionIssuer(
        issuer=ISSUER,
        audience="reports",
        trusted_keys={"test": key.public_key()},
        broker=broker,
        source=lambda w: credential[0],
    )
    worker = ReportWorker(tmp_path / "worker", scheduler=scheduler, token_provider=issuer)
    worker_ref["worker"] = worker
    worker.register(spec.schedule_id, owner, expires_at=datetime.now(UTC) + timedelta(hours=2))
    with worker._db() as db:
        workload = ScheduleWorkload.model_validate_json(
            db.execute("SELECT workload FROM report_jobs").fetchone()[0]
        )
    return worker, workload, broker, issuer, key, credential, owner, authority, approvals


def sign(key, **overrides):
    now = int(datetime.now(UTC).timestamp())
    claims = dict(iss=ISSUER, aud="reports", sub="report-worker", iat=now, exp=now + 600)
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


def test_signed_service_identity_runs_real_fresh_report_and_revokes_session(tmp_path):
    worker, workload, broker, issuer, _, credential, _, _, _ = prepare(tmp_path)
    transport = FakeFeishu()
    backend = FeishuReportBackend(
        target=FeishuTarget(
            app_id="cli_fixture",
            sender_id="ou_bot",
            receiver_type="chat_id",
            receiver_id="oc_fixture",
            configuration_version="v1",
        ),
        transport=transport,
        admitted=lambda: True,
    )
    worker.scheduler.actions.feishu_backends = {("report", "report"): backend}
    issued = []

    def provider(w):
        token = issuer(w)
        issued.append(token)
        identity = broker.authenticate(token)
        assert identity.permissions == frozenset({"business:propose"})
        assert identity.business_constraint is not None
        return token

    worker.token_provider = provider
    assert worker.run_once()[0]["state"] == "active"
    with pytest.raises(PermissionError):
        broker.authenticate(issued[0])
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT state FROM business_actions").fetchone()[0] == "pending"
    assert transport.sends == 0
    with worker.scheduler.actions._db() as db:
        approved_payload = json.loads(
            db.execute("SELECT intent FROM business_actions").fetchone()[0]
        )["payload"]
    assert approved_payload["publication_text"]
    content = broker.path.read_bytes()
    assert credential[0].encode() not in content
    assert issued[0].encode() not in content


def test_revocation_applies_across_processes_and_never_revives_old_session(tmp_path):
    worker, workload, broker, issuer, _, _, _, authority, _ = prepare(tmp_path)
    token = issuer(workload)
    restarted = WorkloadSessionBroker(
        broker.path, allowed=broker.allowed, action_allowed=broker.action_allowed
    )
    assert restarted.authenticate(token).user_id == workload.user_id
    authority["active"] = False
    with pytest.raises(PermissionError):
        restarted.authenticate(token)
    authority["active"] = True
    with pytest.raises(PermissionError):
        broker.authenticate(token)


def test_service_token_cannot_be_authenticated_by_unaware_broker(tmp_path):
    _, workload, broker, issuer, _, _, owner, _, _ = prepare(tmp_path)
    token = issuer(workload)
    unaware = SQLiteSessionBroker(broker.path)
    assert unaware.authenticate(owner).permissions == frozenset(
        {"business:propose", "business:read", "actions:resume"}
    )
    with pytest.raises(PermissionError):
        unaware.authenticate(token)


@pytest.mark.parametrize(
    "bad",
    [
        "subject",
        "audience",
        "issuer",
        "lifetime",
        "expired",
        "signature",
        "unsigned",
        "multi_audience",
    ],
)
def test_untrusted_jwt_claims_cannot_grant_worker_identity(tmp_path, bad):
    _, workload, _, issuer, key, credential, _, _, _ = prepare(tmp_path)
    overrides = {"user_id": "admin", "permissions": ["actions:resume"]}
    if bad == "subject":
        overrides["sub"] = "unapproved-worker"
    elif bad == "audience":
        overrides["aud"] = "different-service"
    elif bad == "issuer":
        overrides["iss"] = "https://untrusted.invalid"
    elif bad == "lifetime":
        overrides["exp"] = int(datetime.now(UTC).timestamp()) + 3600
    elif bad == "expired":
        overrides.update(
            iat=int(datetime.now(UTC).timestamp()) - 600, exp=int(datetime.now(UTC).timestamp()) - 1
        )
    elif bad == "signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif bad == "multi_audience":
        overrides["aud"] = ["reports", "another-service"]
    credential[0] = sign(key, **overrides)
    if bad == "unsigned":
        credential[0] = jwt.encode({"sub": "report-worker"}, key=None, algorithm="none")
    with pytest.raises(PermissionError, match="workload_authentication_failed"):
        issuer(workload)


def test_jwt_actor_and_permission_claims_are_ignored(tmp_path):
    _, workload, broker, issuer, key, credential, _, _, _ = prepare(tmp_path)
    credential[0] = sign(
        key,
        aud=["reports"],
        user_id="administrator",
        tenant_id="other",
        permissions=["actions:resume"],
    )
    identity = broker.authenticate(issuer(workload))
    assert identity.user_id == workload.user_id
    assert identity.tenant_id == workload.tenant_id
    assert identity.permissions == frozenset({"business:propose"})


def test_token_cannot_propose_arbitrary_actions_or_cross_schedule(tmp_path):
    worker, workload, broker, issuer, _, _, _, _, _ = prepare(tmp_path)
    token = issuer(workload)
    runtime = worker.scheduler.actions
    spec = worker._authorized_spec(workload)
    original = worker.scheduler._intent(spec, 0)
    with pytest.raises(PermissionError, match="workload_business_scope_denied"):
        runtime.propose(original.model_copy(update={"destination": "another-destination"}), token)
    with pytest.raises(PermissionError):
        runtime.propose(
            intent(NotificationDraft(message="forged", evidence_hash="sha256:" + "a" * 64)), token
        )
    with pytest.raises(PermissionError):
        worker.scheduler.tick(uuid4(), token)
    other = spec.model_copy(
        update={"schedule_id": uuid4(), "starts_at": runtime._now() + timedelta(minutes=1)}
    )
    # A second schedule has the same owner/scope; the service token still cannot tick it.
    owner = broker.issue(
        user_id=workload.user_id,
        tenant_id=workload.tenant_id,
        project_id=workload.project_id,
        data_domains=frozenset(workload.data_domains),
        permissions=frozenset({"business:propose"}),
    )
    worker.scheduler.register(other, owner)
    assert worker.scheduler.tick(other.schedule_id, owner)["drafts"] == []
    with pytest.raises(PermissionError, match="workload_business_scope_denied"):
        worker.scheduler.tick(other.schedule_id, token)
    with pytest.raises(PermissionError, match="business_permission_denied"):
        runtime.execute(uuid4(), token)
    tampered = workload.model_copy(update={"user_id": "another-owner"})
    with pytest.raises(PermissionError):
        issuer(tampered)
    with sqlite3.connect(runtime.path) as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 0


def test_cancellation_and_job_tampering_revoke_already_issued_session(tmp_path):
    worker, workload, broker, issuer, _, _, owner, _, _ = prepare(tmp_path)
    token = issuer(workload)
    worker.cancel(workload.schedule_id, owner)
    with pytest.raises(PermissionError):
        broker.authenticate(token)


def test_changed_snapshot_query_cannot_use_scoped_service_session(tmp_path):
    worker, workload, broker, issuer, _, _, owner, _, _ = prepare(tmp_path)
    view = worker.scheduler.tick(workload.schedule_id, owner)["drafts"][0]
    action = ActionIntent.model_validate(view["intent"])
    assert worker.authorizes_action(workload, action, None, False)
    raw = runtime_content = worker.scheduler.actions.artifact_reader(
        action.payload.report_artifact, broker.authenticate(owner)
    )
    assert worker.authorizes_action(workload, action, raw, True)
    altered = json.loads(runtime_content)
    altered["query"]["metric_name"] = "different_metric"
    assert not worker.authorizes_action(workload, action, json.dumps(altered).encode(), True)


def test_projected_token_reads_rotation_and_never_persists_credential(tmp_path):
    _, workload, _, issuer, key, _, _, _, _ = prepare(tmp_path)
    path = tmp_path / "mounted.jwt"
    path.write_text(sign(key))
    issuer.source = ProjectedWorkloadToken(path)
    assert issuer(workload)
    path.write_text("replaced-invalid-credential")
    with pytest.raises(PermissionError):
        issuer(workload)


def test_binding_tampering_and_job_mutation_invalidate_issued_tokens(tmp_path):
    worker, workload, broker, issuer, _, _, _, _, _ = prepare(tmp_path)
    token = issuer(workload)
    with sqlite3.connect(broker.path) as db:
        original_body = db.execute("SELECT body FROM workload_bindings").fetchone()[0]
        body = json.loads(original_body)
        body["subject"] = "changed-worker"
        db.execute("UPDATE workload_bindings SET body=?", (json.dumps(body),))
    with pytest.raises(PermissionError, match="workload_session_denied"):
        broker.authenticate(token)
    with sqlite3.connect(broker.path) as db:
        db.execute("UPDATE workload_bindings SET body=?", (original_body,))
    with pytest.raises(PermissionError):
        broker.authenticate(token)
    next_token = issuer(workload)
    with worker._db() as db, db:
        db.execute(
            "UPDATE report_jobs SET workload=?",
            (workload.model_copy(update={"schedule_hash": "changed"}).model_dump_json(),),
        )
    with pytest.raises(PermissionError):
        broker.authenticate(next_token)


def test_token_selected_key_urls_and_unknown_key_ids_are_rejected(tmp_path):
    _, workload, _, issuer, key, credential, _, _, _ = prepare(tmp_path)
    claims = jwt.decode(credential[0], key.public_key(), algorithms=["RS256"], audience="reports")
    for header in ({"kid": "test", "jku": "https://untrusted.invalid/keys"}, {"kid": "unknown"}):
        credential[0] = jwt.encode(claims, key, algorithm="RS256", headers=header)
        with pytest.raises(PermissionError, match="workload_authentication_failed"):
            issuer(workload)


def test_delegated_token_cannot_cancel_unrelated_owner_draft_or_submit_feedback(tmp_path):
    from fastapi.testclient import TestClient

    from generaldataagent.api.app import create_app

    worker, workload, broker, issuer, _, _, owner, _, _ = prepare(tmp_path)
    runtime = worker.scheduler.actions
    spec = worker._authorized_spec(workload)
    unrelated = intent(spec.report)
    view = runtime.propose(unrelated, owner)
    token = issuer(workload)
    with pytest.raises(PermissionError, match="workload_business_scope_denied"):
        runtime.cancel(unrelated.action_id, token)
    assert runtime.get(unrelated.action_id, owner)["state"] == view["state"] == "pending"
    app = TestClient(
        create_app(
            session_broker=broker, business_actions=runtime, report_scheduler=worker.scheduler
        )
    )
    headers = {
        "authorization": "Bearer " + token,
        "x-user-id": workload.user_id,
        "x-tenant-id": workload.tenant_id,
        "x-project-id": workload.project_id,
        "x-data-domain": workload.data_domains[0],
    }
    response = app.post("/v1/business/feedback", json={}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "service_session_route_denied"
    # The intended route is usable with the same token and generates a pending report.
    response = app.post(f"/v1/business/schedules/{workload.schedule_id}/tick", headers=headers)
    assert response.status_code == 200
    assert response.json()["drafts"][0]["state"] == "pending"


def test_control_scope_change_revokes_existing_service_session(tmp_path):
    from dataclasses import replace

    worker, workload, broker, issuer, _, _, _, _, _ = prepare(
        tmp_path, domains=("commerce", "finance")
    )
    token = issuer(workload)
    runtime = worker.scheduler.actions
    runtime.controls[("report", "report")] = replace(
        runtime.controls[("report", "report")], data_domains=frozenset({"commerce"})
    )
    with pytest.raises(PermissionError, match="workload_session_denied"):
        broker.authenticate(token)


def test_delegation_revoked_during_generation_cannot_propose_report(tmp_path, monkeypatch):
    worker, _, _, _, _, _, _, authority, _ = prepare(tmp_path)
    original = worker.scheduler.refresher.prepare

    def revoked_during_generation(**kwargs):
        ref = original(**kwargs)
        authority["active"] = False
        return ref

    monkeypatch.setattr(worker.scheduler.refresher, "prepare", revoked_during_generation)
    assert worker.run_once()[0]["state"] == "failed"
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 0
