"""Synthetic signing/HTTP fixtures exercise assembly; none is real production evidence."""

import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_stage5_actions import PAYLOADS, intent
from test_stage5_approval_service import authority as authority_fixture
from test_stage5_approval_service import review
from test_stage5_github_reviews import ReviewGitHub

from generaldataagent.api.app import create_app
from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.business.github_review import GitHubReviewBackend, GitHubReviewTarget
from generaldataagent.business.production import (
    QualifiedBusinessActionRuntime,
    business_configuration_hash,
)
from generaldataagent.evaluation.stage_five_external import (
    QualifiedActionBundle,
    action_qualification_statement,
)
from generaldataagent.observability.http import AuditHTTPTransport
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.policy.remote_approvals import HTTPSApprovalTransport, RemoteApprovalStore
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.business_admission import FileActionAdmission
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.security.release import REQUIRED_EXERCISES, ReleaseEvidence, SLOEvidence
from generaldataagent.security.release_bundle import (
    ReleaseBundle,
    ReleaseSignature,
    release_statement,
)


@pytest.fixture
def qualified(tmp_path):
    from test_stage5_acceptance import signed_bundle

    state = tmp_path / "protected"
    state.mkdir(mode=0o700)
    a = authority_fixture.__wrapped__(tmp_path / "authority")

    class ApprovalHTTP(HTTPSApprovalTransport):
        def __call__(self, *args):
            return a.transport(*args)

    approval_http = ApprovalHTTP(origin="https://authority.invalid")
    approvals = RemoteApprovalStore(transport=approval_http, credential=lambda _: a.alice)
    captured = []
    outage = [False]
    audit_hook = [lambda value: None]
    audit_key = Ed25519PrivateKey.generate()

    class AuditHTTP(AuditHTTPTransport):
        def __call__(self, content):
            if outage[0]:
                raise PermissionError("isolated fixture outage")
            body = json.loads(content)
            captured.append(body)
            audit_hook[0](body)
            receipt = AuditReceipt(
                event_id=body["event_id"],
                event_sha256=hashlib.sha256(content).hexdigest(),
                sequence=len(captured),
                recorded_at=datetime.now(UTC),
                signer_id="audit-fixture",
                signature="",
            )
            return receipt.model_copy(
                update={
                    "signature": base64.b64encode(
                        audit_key.sign(receipt_statement(receipt))
                    ).decode()
                }
            )

    audit = VerifiedAuditSink(
        transport=AuditHTTP(
            endpoint="https://audit.invalid/append", bearer_token=SecretStr("x" * 32)
        ),
        trusted_keys={"audit-fixture": audit_key.public_key()},
        pseudonym_key=b"p" * 32,
    )
    broker = SQLiteSessionBroker(state / "sessions")
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    fake = ReviewGitHub()
    backend = GitHubReviewBackend(
        target=GitHubReviewTarget(
            repository="acme/reviews",
            repository_id=22,
            actor_id=11,
            kind="ticket",
            configuration_version="v1",
        ),
        transport=fake,
        admitted=lambda: True,
    )
    control = ActionControl(
        "ticket", "ticket", "operator", "t", "p", frozenset({"sales"}), enabled=True
    )
    config = business_configuration_hash(
        control=control,
        backend=backend,
        approval_origin=approval_http.origin,
        service_version="v1",
        audit=audit,
    )
    now = datetime.now(UTC)
    keys = {
        ("fixture-security", "security"): Ed25519PrivateKey.generate(),
        ("fixture-operations", "operations"): Ed25519PrivateKey.generate(),
    }
    trust = json.dumps(
        [
            {
                "signer_id": name,
                "role": role,
                "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            }
            for (name, role), key in keys.items()
        ]
    )
    signatures = tuple(
        ReleaseSignature(signer_id=name, role=role, signature="") for name, role in keys
    )
    release_bundle = ReleaseBundle(
        environment="staging",
        evidence=ReleaseEvidence(
            "a" * 40,
            now,
            100,
            88,
            0,
            0,
            0,
            100,
            99,
            REQUIRED_EXERCISES,
            slo=SLOEvidence(30 * 86400, 0, 100, 2.0, 60.0, 14),
        ),
        valid_until=now + timedelta(hours=1),
        signatures=signatures,
    )

    def sign(bundle, statement):
        raw = statement(bundle)
        return bundle.model_copy(
            update={
                "signatures": tuple(
                    s.model_copy(
                        update={
                            "signature": base64.b64encode(
                                keys[(s.signer_id, s.role)].sign(raw)
                            ).decode()
                        }
                    )
                    for s in signatures
                )
            }
        )

    release_bundle = sign(release_bundle, release_statement)
    release_path, release_trust = state / "release.json", state / "release-trust.json"
    release_path.write_text(release_bundle.model_dump_json())
    release_trust.write_text(trust)
    release = FileReleaseAdmission(
        bundle_path=release_path, trust_path=release_trust, revision="a" * 40, environment="staging"
    )
    template, _ = signed_bundle()
    bundle = QualifiedActionBundle(
        evidence=template.evidence.model_copy(
            update={
                "kind": "ticket",
                "destination": "ticket",
                "owner": "operator",
                "configuration_hash": config,
            }
        ),
        signatures=signatures,
    )
    bundle = sign(bundle, action_qualification_statement)
    action_path, action_trust = state / "action.json", state / "action-trust.json"
    action_path.write_text(bundle.model_dump_json())
    action_trust.write_text(trust)
    admission = FileActionAdmission(
        bundle_path=action_path,
        trust_path=action_trust,
        kind="ticket",
        destination="ticket",
        owner="operator",
        revision="a" * 40,
        environment="staging",
        configuration_hash=config,
    )
    ports = dict(
        broker=broker,
        approvals=approvals,
        controls=(control,),
        external_backends={("ticket", "ticket"): backend},
    )
    production = dict(
        audit=audit,
        release=release,
        admissions={("ticket", "ticket"): admission},
        service_version="v1",
    )
    runtime = QualifiedBusinessActionRuntime(state / "actions", **production, **ports)
    return SimpleNamespace(**locals())


def propose(q):
    action = intent(PAYLOADS[1])
    view = q.runtime.propose(action, q.token)
    approval = q.a.store.inspect(UUID(view["approval_id"])).request
    assert review(q.a, approval).status_code == 200
    return action, view


def test_qualified_api_requires_remote_approval_and_independent_signed_history(qualified):
    q = qualified
    assert q.runtime.production_ready()
    client = TestClient(
        create_app(session_broker=q.broker, business_actions=q.runtime, production_mode=True)
    )
    action, _ = propose(q)
    result = q.runtime.execute(action.action_id, q.token)
    assert result["receipt"]["external_delivery"] is True
    assert q.runtime.verify_audit()
    assert len(q.fake.writes) == 1
    assert q.runtime.execute(action.action_id, q.token) == result
    with q.runtime._db() as db:
        assert db.execute("SELECT count(*) FROM business_events").fetchone()[0] == len(q.captured)
        assert db.execute("SELECT count(*) FROM business_audit_receipts").fetchone()[0] == len(
            q.captured
        )
    assert all(
        "exact" not in v["payload"] and set(v["payload"]) == {"request_fingerprint"}
        for v in q.captured
    )
    assert client.get("/business").status_code == 200
    restarted = QualifiedBusinessActionRuntime(q.runtime.path, **q.production, **q.ports)
    assert restarted.get(action.action_id, q.token) == result


@pytest.mark.parametrize(
    "mutation", ["release", "action", "configuration", "audit_keys", "permissions", "proof_scope"]
)
def test_live_proof_configuration_and_state_revocation_stops_writes(qualified, mutation):
    q = qualified
    action, _ = propose(q)
    if mutation == "release":
        q.release_trust.write_text("[]")
    elif mutation == "action":
        q.action_trust.write_text("[]")
    elif mutation == "configuration":
        q.runtime.controls[("ticket", "ticket")] = replace(q.control, per_minute=11)
    elif mutation == "audit_keys":
        q.audit.trusted_keys.clear()
    elif mutation == "permissions":
        os.chmod(q.runtime.path, 0o644)
    else:
        q.admission.destination = "other"
    assert not q.runtime.production_ready()
    with pytest.raises((PermissionError, ValueError)):
        q.runtime.execute(action.action_id, q.token)
    assert not q.fake.writes


def test_revoked_destination_after_approval_consumption_blocks_external_write(qualified):
    q = qualified
    action, _ = propose(q)

    def revoke(body):
        if body["event_type"] == "approval.consumed":
            q.action_trust.write_text("[]")

    q.audit_hook[0] = revoke
    with pytest.raises(PermissionError):
        q.runtime.execute(action.action_id, q.token)
    assert not q.fake.writes


def test_independent_audit_failure_prevents_claim_or_remote_write(qualified):
    q = qualified
    action, _ = propose(q)
    q.outage[0] = True
    with pytest.raises(PermissionError):
        q.runtime.execute(action.action_id, q.token)
    assert not q.fake.writes
    assert q.runtime.get(action.action_id, q.token)["state"] == "pending"


@pytest.mark.parametrize("column,value", [("canonical", "{}"), ("receipt", "{}")])
def test_local_audit_rehash_cannot_replace_independent_acknowledgement(qualified, column, value):
    q = qualified
    action, _ = propose(q)
    with q.runtime._db() as db, db:
        db.execute(f"UPDATE business_audit_receipts SET {column}=?", (value,))
    assert not q.runtime.verify_audit()
    with pytest.raises(PermissionError):
        q.runtime.get(action.action_id, q.token)
    assert not q.fake.writes


def test_pilot_history_and_unqualified_runtime_remain_rejected(qualified):
    q = qualified
    path = q.state / "pilot"
    pilot = BusinessActionRuntime(path, **q.ports)
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="cannot be promoted"):
        QualifiedBusinessActionRuntime(path, **q.production, **q.ports)
    with pytest.raises(ValueError, match="not qualified"):
        create_app(session_broker=q.broker, business_actions=pilot, production_mode=True)


@pytest.mark.parametrize(
    "mutation", ["resource_revision", "resource_owner", "action_state", "action_scope"]
)
def test_local_state_cannot_diverge_from_signed_history(qualified, mutation):
    q = qualified
    action, _ = propose(q)
    q.runtime.execute(action.action_id, q.token)
    with q.runtime._db() as db, db:
        if mutation == "resource_revision":
            db.execute("UPDATE business_resources SET revision=99")
        elif mutation == "resource_owner":
            db.execute("UPDATE business_resources SET action_id='different'")
        elif mutation == "action_state":
            db.execute("UPDATE business_actions SET state='pending'")
        else:
            row = db.execute("SELECT request FROM business_actions").fetchone()
            request = json.loads(row[0])
            request["project_id"] = "other"
            db.execute(
                "UPDATE business_actions SET project=?,request=?", ("other", json.dumps(request))
            )
    assert not q.runtime.verify_audit()
    with pytest.raises(PermissionError):
        q.runtime.get(action.action_id, q.token)
    assert len(q.fake.writes) == 1


def test_qualified_compensation_preserves_audited_resource_revision(qualified):
    q = qualified
    action, _ = propose(q)
    q.runtime.execute(action.action_id, q.token)
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = q.runtime.propose(undo, q.token)
    assert review(q.a, q.a.store.inspect(UUID(view["approval_id"])).request).status_code == 200
    result = q.runtime.execute(undo.action_id, q.token)
    assert result["receipt"]["external"]["state"] == "recalled"
    assert result["audit_acknowledgement"]["receipt"]["signature"]
    assert q.runtime.verify_audit()
    assert len(q.fake.writes) == 2


def test_released_factory_requires_the_same_business_source_context(qualified):
    from generaldataagent.api.production import create_released_app

    q = qualified
    assert create_released_app(
        admission=q.release,
        session_broker=q.broker,
        service_enabled=lambda: True,
        business_actions=q.runtime,
    )
    q.runtime.release.environment = "production"
    separate = FileReleaseAdmission(
        bundle_path=q.release_path,
        trust_path=q.release_trust,
        revision="a" * 40,
        environment="staging",
    )
    with pytest.raises(ValueError, match="context must match"):
        create_released_app(
            admission=separate,
            session_broker=q.broker,
            service_enabled=lambda: True,
            business_actions=q.runtime,
        )


def test_delivery_ack_loss_during_completion_is_reconciled_without_resend(qualified):
    q = qualified
    action, _ = propose(q)

    def lost_ack(body):
        if len(q.captured) == 5:
            raise TimeoutError("isolated lost completion audit ack")

    q.audit_hook[0] = lost_ack
    with pytest.raises(PermissionError):
        q.runtime.execute(action.action_id, q.token)
    assert q.runtime.get(action.action_id, q.token)["state"] == "uncertain"
    assert len(q.fake.writes) == 1
    assert q.runtime.reconcile(action.action_id, q.token, "1")["state"] == "completed"
    assert q.runtime.verify_audit()
    assert len(q.fake.writes) == 1


def test_cancel_and_expired_or_malformed_action_qualification(qualified):
    q = qualified
    action, _ = propose(q)
    assert q.runtime.cancel(action.action_id, q.token)["state"] == "cancelled"
    assert q.runtime.verify_audit()
    assert not q.admission.allowed(now=datetime.now(UTC) + timedelta(days=2))
    q.action_path.write_text("{}")
    assert not q.admission.allowed()
    assert not q.runtime.production_ready()


def test_production_authority_requires_fresh_private_independently_initialized_state(qualified):
    from generaldataagent.services.approval_service import create_approval_service

    q = qualified
    q.a.store.path.parent.chmod(0o700)
    q.a.store.path.chmod(0o600)
    arguments = dict(
        store=q.a.store,
        broker=q.broker,
        audit=q.audit,
        admission=q.release,
        reviewer_admitted=lambda who, request: who.user_id == "bob",
        grant_admitted=lambda who, request: who == "bob",
    )
    app = create_approval_service(**arguments)
    assert TestClient(app).get("/review").status_code == 200
    assert create_approval_service(**arguments)
    with q.a.store._connect() as db:
        db.execute("UPDATE approval_authority_profile SET receipt='{}'")
    assert TestClient(app).get("/review").status_code == 503


def test_old_local_authority_grants_cannot_enter_production(qualified):
    from test_stage5_approval_service import create

    from generaldataagent.services.approval_service import create_approval_service

    q = qualified
    create(q.a)
    q.a.store.path.parent.chmod(0o700)
    q.a.store.path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be promoted"):
        create_approval_service(
            store=q.a.store,
            broker=q.broker,
            audit=q.audit,
            admission=q.release,
            reviewer_admitted=lambda *_: True,
            grant_admitted=lambda *_: True,
        )
