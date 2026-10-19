import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.contracts import ToolRequest
from generaldataagent.policy.approvals import ApprovalError, SQLiteApprovalStore
from generaldataagent.policy.remote_approvals import HTTPSApprovalTransport, RemoteApprovalStore
from generaldataagent.security.identity import SessionBroker
from generaldataagent.services.approval_service import create_approval_service


@pytest.fixture
def authority(tmp_path):
    broker = SessionBroker()
    store = SQLiteApprovalStore(tmp_path / "authority.db")
    events = []
    enrolled = {"bob"}
    enabled = [True]
    audit_hook = [lambda event: None]

    class Audit:
        def emit(self, event):
            audit_hook[0](event)
            events.append(event)

    def issue(user, tenant="t", domains=frozenset({"sales"}), permissions=None):
        return broker.issue(
            user_id=user,
            tenant_id=tenant,
            project_id="p",
            data_domains=domains,
            permissions=permissions or frozenset({"approvals:request", "approvals:review"}),
        )

    alice, bob, eve = issue("alice"), issue("bob"), issue("eve")
    app = create_approval_service(
        store=store,
        broker=broker,
        reviewer_admitted=lambda who, req: who.user_id in enrolled,
        grant_admitted=lambda who, req: who in enrolled,
        audit=Audit(),
        service_enabled=lambda: enabled[0],
        production_mode=False,
    )
    client = TestClient(app)

    def transport(method, path, body, token):
        response = client.request(
            method, path, json=body, headers={"authorization": f"Bearer {token}"}
        )
        if response.status_code >= 400:
            raise ApprovalError("service denied")
        return response.json()

    remote = RemoteApprovalStore(transport=transport, credential=lambda _: alice)
    request = ToolRequest(
        task_id=uuid4(),
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=("sales",),
        agent_role="reporter",
        tool_name="business.draft",
        action="deliver",
        resource="ticket",
        arguments={"exact": "frozen"},
        requested_risk="r2",
    )
    expiry = datetime.now(UTC) + timedelta(hours=1)
    return SimpleNamespace(**locals())


def create(a):
    return a.remote.create_request(
        a.request,
        impact_scope="Create frozen ticket",
        cost_estimate="One write",
        classification="internal",
        rollback_plan="Separate approval",
        expires_at=a.expiry,
    )


def review(a, approval, token=None, **changes):
    return a.client.post(
        f"/v1/approval-reviews/{approval.approval_id}/approve",
        headers={"authorization": f"Bearer {token or a.bob}"},
        json={"request_fingerprint": approval.request_fingerprint, "confirmed": True, **changes},
    )


def test_remote_one_use_approval_requires_real_authenticated_reviewer(authority):
    a = authority
    approval = create(a)
    with pytest.raises(ApprovalError):
        a.remote.validate(approval.approval_id, a.request)
    assert review(a, approval, a.alice).status_code == 403
    assert review(a, approval, a.eve).status_code == 403
    assert review(a, approval, request_fingerprint="sha256:" + "f" * 64).status_code == 409
    assert review(a, approval, confirmed=False).status_code == 409
    assert review(a, approval).status_code == 200
    assert a.remote.validate(approval.approval_id, a.request).approver_id == "bob"
    a.remote.consume(approval.approval_id, a.request)
    with pytest.raises(ApprovalError):
        a.remote.consume(approval.approval_id, a.request)
    assert a.store.inspect(approval.approval_id).consumed
    assert any(e.event_type == "approval.consumed" for e in a.events)
    assert all(set(e.payload) == {"request_fingerprint"} for e in a.events)
    with pytest.raises(ApprovalError):
        a.remote.approve(approval.approval_id, approver_id="bob")


@pytest.mark.parametrize("change", ["tenant", "domains", "requester", "fingerprint"])
def test_foreign_scope_and_changed_request_denied(authority, change):
    a = authority
    approval = create(a)
    values = {
        "tenant": {"tenant_id": "other"},
        "domains": {"data_domains": ("restricted",)},
        "requester": {"user_id": "eve"},
        "fingerprint": {"arguments": {"exact": "changed"}},
    }[change]
    changed = a.request.model_copy(update=values)
    with pytest.raises(ApprovalError):
        a.remote.validate(approval.approval_id, changed)
    assert not a.store.inspect(approval.approval_id).grant


def test_reviewer_enrollment_revocation_and_exact_queue(authority):
    a = authority
    approval = create(a)
    url = "/v1/approval-requests"
    headers = {"authorization": f"Bearer {a.bob}"}
    page = a.client.get(url, headers=headers).json()
    assert page["items"][0]["request"]["parameters"] == a.request.arguments
    assert page["items"][0]["request"]["data_domains"] == ["sales"]
    a.enrolled.clear()
    assert a.client.get(url, headers=headers).json()["items"] == []
    assert review(a, approval).status_code == 403
    assert (
        a.client.get(
            url, headers={"authorization": f"Bearer {a.issue('foreign', tenant='other')}"}
        ).json()["items"]
        == []
    )


def test_session_revoked_during_audit_cannot_grant(authority):
    a = authority
    approval = create(a)
    a.audit_hook[0] = lambda _: a.broker.revoke(a.bob)
    assert review(a, approval).status_code == 403
    assert a.store.inspect(approval.approval_id).grant is None


def test_audit_failure_or_kill_switch_cannot_grant(authority):
    a = authority
    approval = create(a)

    def fail(_):
        raise RuntimeError("sensitive audit diagnostic")

    a.audit_hook[0] = fail
    response = review(a, approval)
    assert response.status_code == 503 and "sensitive" not in response.text
    assert a.store.inspect(approval.approval_id).grant is None
    a.enabled[0] = False
    assert review(a, approval).status_code == 503


def test_cancellation_rejection_and_requester_inspection(authority):
    a = authority
    approval = create(a)
    assert (
        a.client.get(
            f"/v1/approval-requests/{approval.approval_id}",
            headers={"authorization": f"Bearer {a.eve}"},
        ).status_code
        == 403
    )
    result = a.remote.cancel(approval.approval_id, requester_id="alice", reason="Cancel")
    assert result.disposition == "cancelled"
    assert review(a, approval).status_code == 409
    a.request = a.request.model_copy(update={"arguments": {"exact": "second action"}})
    second = create(a)
    response = a.client.post(
        f"/v1/approval-reviews/{second.approval_id}/reject",
        headers={"authorization": f"Bearer {a.bob}"},
        json={
            "request_fingerprint": second.request_fingerprint,
            "confirmed": True,
            "reason": "Evidence missing",
        },
    )
    assert response.status_code == 200
    with pytest.raises(ApprovalError):
        a.remote.validate(second.approval_id, a.request)


def test_request_body_bounds_and_no_unreviewed_production(authority):
    a = authority
    assert a.client.post("/v1/approval-requests", content=b"x" * 262145).status_code == 413
    with pytest.raises(ValueError):
        create_approval_service(
            store=a.store, broker=a.broker, reviewer_admitted=lambda *_: True, audit=a.Audit()
        )


def test_transport_requires_verified_https_and_forbids_review_mutations():
    for origin in [
        "http://example.org",
        "https://user:secret@example.org",
        "https://example.org/path",
        "https://example.org?x=1",
    ]:
        with pytest.raises(ValueError):
            HTTPSApprovalTransport(origin=origin)
    with pytest.raises(ValueError):
        HTTPSApprovalTransport(
            origin="https://example.org", context=ssl._create_unverified_context()
        )
    transport = HTTPSApprovalTransport(origin="https://example.org")
    with pytest.raises(ValueError):
        transport("POST", f"/v1/approval-reviews/{uuid4()}/approve", {}, "secret")


def test_runtime_uses_remote_authority_without_local_grant_capability(authority, tmp_path):
    from test_stage5_actions import PAYLOADS, intent

    from generaldataagent.business.actions import ActionControl, BusinessActionRuntime

    a = authority
    token = a.broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:read", "business:propose", "actions:resume"}),
    )
    runtime = BusinessActionRuntime(
        tmp_path / "outbox",
        broker=a.broker,
        approvals=a.remote,
        controls=(
            ActionControl("ticket", "ticket", "ops", "t", "p", frozenset({"sales"}), enabled=True),
        ),
    )
    action = intent(PAYLOADS[1])
    view = runtime.propose(action, token)
    approval = a.store.inspect(UUID(view["approval_id"])).request
    assert review(a, approval).status_code == 200
    assert runtime.execute(action.action_id, token)["state"] == "completed"
    assert a.store.inspect(approval.approval_id).consumed


def test_remote_response_tampering_and_no_local_review_authority(authority):
    a = authority
    original = a.remote.transport

    def changed(method, path, body, token):
        result = original(method, path, body, token)
        result["resource"] = "different"
        return result

    a.remote.transport = changed
    with pytest.raises(ApprovalError, match="binding_changed"):
        create(a)
    a.remote.transport = original
    approval = create(a)
    assert review(a, approval).status_code == 200
    a.remote.transport = lambda *args: {"invalid": "raw secret diagnostic"}
    with pytest.raises(ApprovalError, match="response_invalid"):
        a.remote.validate(approval.approval_id, a.request)
    with pytest.raises(ApprovalError):
        a.remote.reject(approval.approval_id, reviewer_id="bob", reason="Reject")


def test_requester_service_delegation_is_explicit_and_revocable(authority):
    a = authority
    service = a.issue("worker", permissions=frozenset({"approvals:request", "approvals:delegate"}))
    a.remote.credential = lambda _: service
    with pytest.raises(ApprovalError):
        create(a)
    permitted = [True]
    app = create_approval_service(
        store=a.store,
        broker=a.broker,
        reviewer_admitted=lambda who, req: who.user_id == "bob",
        audit=a.Audit(),
        production_mode=False,
        delegation_allowed=lambda who, user: (
            who.user_id == "worker" and user == "alice" and permitted[0]
        ),
    )
    client = TestClient(app)

    def transport(method, path, body, token):
        r = client.request(method, path, json=body, headers={"authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise ApprovalError("denied")
        return r.json()

    a.remote.transport = transport
    approval = create(a)
    permitted[0] = False
    with pytest.raises(ApprovalError):
        a.remote.validate(approval.approval_id, a.request)


def test_reviewer_assets_have_no_live_credentials(authority):
    a = authority
    for path in ["/review", "/review/console.js", "/review/console.css"]:
        response = a.client.get(path)
        assert response.status_code == 200
        assert a.bob not in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"


def test_https_transport_stdin_equivalent_bounds_and_no_retry(monkeypatch):
    import json
    from urllib.error import HTTPError

    t = HTTPSApprovalTransport(origin="https://authority.example")
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"ok": True}).encode()

    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 10
        assert request.full_url == "https://authority.example/v1/approval-requests"
        assert request.get_header("Authorization") == "Bearer private"
        return Response()

    monkeypatch.setattr(t.opener, "open", open_request)
    assert t("POST", "/v1/approval-requests", {"exact": "body"}, "private") == {"ok": True}
    assert json.loads(calls[0].data) == {"exact": "body"}

    def fail(*args, **kwargs):
        calls.append("failure")
        raise HTTPError("https://authority.example", 503, "secret diagnostic", {}, None)

    monkeypatch.setattr(t.opener, "open", fail)
    with pytest.raises(ApprovalError, match="remote_approval_denied"):
        t("POST", "/v1/approval-requests", {}, "private")
    assert len(calls) == 2


def test_business_approval_fence_reuses_pending_and_consumed_request(authority):
    a = authority
    expiry = datetime.now(UTC) + timedelta(hours=1)
    metadata = dict(
        impact_scope="Frozen action",
        cost_estimate="One call",
        classification="internal",
        rollback_plan="Separate request",
        expires_at=expiry,
    )
    first = a.remote.create_request(a.request, **metadata)
    assert a.remote.create_request(a.request, **metadata) == first
    assert review(a, first).status_code == 200
    a.remote.consume(first.approval_id, a.request)
    assert a.remote.create_request(a.request, **metadata) == first
    with pytest.raises(ApprovalError):
        a.remote.validate(first.approval_id, a.request)
    with pytest.raises(ApprovalError):
        a.remote.create_request(a.request, **{**metadata, "impact_scope": "Different scope"})


def test_action_uuid_cannot_acquire_a_second_approval_for_changed_content(authority):
    a = authority
    a.request = a.request.model_copy(
        update={"arguments": {"action_id": str(uuid4()), "exact": "original"}}
    )
    first = create(a)
    a.request = a.request.model_copy(
        update={"arguments": {**a.request.arguments, "exact": "changed"}}
    )
    with pytest.raises(ApprovalError):
        create(a)
    assert a.store.inspect(first.approval_id).request.parameters["exact"] == "original"


@pytest.mark.parametrize("timing", ["before", "during_audit"])
def test_revoked_reviewer_enrollment_invalidates_outstanding_grant(authority, timing):
    a = authority
    approval = create(a)
    assert review(a, approval).status_code == 200
    if timing == "before":
        a.enrolled.clear()
    else:
        a.audit_hook[0] = lambda _: a.enrolled.clear()
    with pytest.raises(ApprovalError):
        a.remote.validate(approval.approval_id, a.request)
    with pytest.raises(ApprovalError):
        a.remote.consume(approval.approval_id, a.request)
    assert not a.store.inspect(approval.approval_id).consumed
