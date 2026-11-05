"""Requester-scoped approval todo reads: exact facts, no approval authority."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.contracts import ToolRequest
from generaldataagent.policy.approvals import SQLiteApprovalStore, approval_status
from generaldataagent.security.identity import SessionBroker
from generaldataagent.services.approval_service import create_approval_service


@pytest.fixture
def authority(tmp_path):
    broker = SessionBroker()
    clock = [datetime.now(UTC)]
    store = SQLiteApprovalStore(tmp_path / "authority.db", clock=lambda: clock[0])
    events = []

    class Audit:
        def emit(self, event):
            events.append(event)

    def issue(user, *, domains=frozenset({"sales"}), permissions=None):
        return broker.issue(
            user_id=user,
            tenant_id="t",
            project_id="p",
            data_domains=domains,
            permissions=permissions
            if permissions is not None
            else frozenset({"approvals:request", "approvals:review"}),
        )

    alice, bob, reader = (
        issue("alice"),
        issue("bob"),
        issue("reader", permissions=frozenset({"business:read"})),
    )
    app = create_approval_service(
        store=store,
        broker=broker,
        reviewer_admitted=lambda who, req: who.user_id == "bob",
        audit=Audit(),
        production_mode=False,
    )
    return SimpleNamespace(
        **locals(),
        client=TestClient(app),
        headers=lambda token: {"authorization": f"Bearer {token}"},
    )


def tool_request(user="alice", *, domains=("sales",)):
    return ToolRequest(
        task_id=uuid4(),
        user_id=user,
        tenant_id="t",
        project_id="p",
        data_domains=domains,
        agent_role="reporter",
        tool_name="business.draft",
        action="deliver",
        resource=f"ticket-{uuid4()}",
        arguments={"exact": "frozen"},
        requested_risk="r2",
    )


def create(a, request=None, *, expires_in=timedelta(hours=1)):
    request = request or tool_request()
    response = a.client.post(
        "/v1/approval-requests",
        headers=a.headers(a.alice),
        json={
            "request": request.model_dump(mode="json"),
            "impact_scope": "Create frozen ticket",
            "cost_estimate": "One write",
            "classification": "internal",
            "rollback_plan": "Separate approval",
            "expires_at": (a.clock[0] + expires_in).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return request, response.json()


def seed(a, request, *, expires_in=timedelta(hours=1)):
    """Trusted fixture operator: other users' rows never come from alice's token."""
    return a.store.create_request(
        request,
        impact_scope="Create frozen ticket",
        cost_estimate="One write",
        classification="internal",
        rollback_plan="Separate approval",
        expires_at=a.clock[0] + expires_in,
    ).model_dump(mode="json")


def mine(a, token=None, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return a.client.get(
        "/v1/approval-requests/mine" + (f"?{query}" if query else ""),
        headers=a.headers(token or a.alice),
    )


def approve(a, approval):
    response = a.client.post(
        f"/v1/approval-reviews/{approval['approval_id']}/approve",
        headers=a.headers(a.bob),
        json={"request_fingerprint": approval["request_fingerprint"], "confirmed": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def reject(a, approval):
    response = a.client.post(
        f"/v1/approval-reviews/{approval['approval_id']}/reject",
        headers=a.headers(a.bob),
        json={
            "request_fingerprint": approval["request_fingerprint"],
            "confirmed": True,
            "reason": "Out of policy",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_requester_sees_only_own_exact_scope(authority):
    a = authority
    _, mine_one = create(a)
    _, mine_two = create(a)
    foreign_user = seed(a, tool_request("bob"))
    foreign_domain = seed(a, tool_request(domains=("restricted",)))
    foreign_tenant = seed(
        a, tool_request().model_copy(update={"tenant_id": "other", "project_id": "other"})
    )

    response = mine(a)
    assert response.status_code == 200, response.text
    page = response.json()
    ids = [item["approval_id"] for item in page["items"]]
    assert ids == [mine_two["approval_id"], mine_one["approval_id"]]
    assert foreign_user["approval_id"] not in ids
    assert foreign_domain["approval_id"] not in ids
    assert foreign_tenant["approval_id"] not in ids
    assert page["schema_version"] == "1.0"


def test_reduced_projection_never_carries_parameters_or_rollback(authority):
    a = authority
    request, approval = create(a)
    page = mine(a).json()
    item = page["items"][0]
    assert item["approval_id"] == approval["approval_id"]
    assert item["request_fingerprint"] == approval["request_fingerprint"]
    assert item["task_id"] == str(request.task_id)
    assert item["tool_name"] == "business.draft"
    assert item["action"] == "deliver"
    assert item["resource"] == request.resource
    assert item["data_domains"] == ["sales"]
    assert item["classification"] == "internal"
    assert item["impact_scope"] == "Create frozen ticket"
    assert item["cost_estimate"] == "One write"
    assert item["state"] == "pending"
    assert item["decided_at"] is None
    assert item["decided_by"] is None
    assert item["reason"] is None
    assert "parameters" not in item
    assert "rollback_plan" not in item


def test_states_follow_grants_dispositions_consumption_and_expiry(authority):
    a = authority
    _, pending = create(a)
    _, approved = create(a)
    approve(a, approved)
    _, rejected = create(a)
    reject(a, rejected)
    _, cancelled = create(a)
    response = a.client.post(
        f"/v1/approval-cancellations/{cancelled['approval_id']}",
        headers=a.headers(a.alice),
    )
    assert response.status_code == 200, response.text
    request, consumed = create(a)
    approve(a, consumed)
    response = a.client.post(
        f"/v1/approval-requests/{consumed['approval_id']}/consume",
        headers=a.headers(a.alice),
        json={"request": request.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text

    states = {item["approval_id"]: item for item in mine(a).json()["items"]}
    assert states[pending["approval_id"]]["state"] == "pending"
    assert states[approved["approval_id"]]["state"] == "approved"
    assert states[approved["approval_id"]]["decided_by"] == "bob"
    assert states[rejected["approval_id"]]["state"] == "rejected"
    assert states[rejected["approval_id"]]["reason"] == "Out of policy"
    assert states[cancelled["approval_id"]]["state"] == "cancelled"
    assert states[cancelled["approval_id"]]["decided_by"] == "alice"
    assert states[consumed["approval_id"]]["state"] == "consumed"

    a.clock[0] += timedelta(hours=2)
    expired = {item["approval_id"]: item for item in mine(a).json()["items"]}
    assert expired[pending["approval_id"]]["state"] == "expired"
    assert expired[pending["approval_id"]]["decided_at"] is None
    assert expired[approved["approval_id"]]["state"] == "expired"
    assert expired[approved["approval_id"]]["decided_by"] == "bob"
    assert expired[consumed["approval_id"]]["state"] == "consumed"
    assert expired[rejected["approval_id"]]["state"] == "rejected"


def test_permission_bounds_and_cursor_paging(authority):
    a = authority
    created = [create(a)[1]["approval_id"] for _ in range(3)]

    denied = mine(a, a.reader)
    assert denied.status_code == 403
    assert (
        a.client.get(
            "/v1/approval-requests/mine", headers={"authorization": "Bearer wrong"}
        ).status_code
        == 403
    )
    assert mine(a, limit=0).status_code == 422
    assert mine(a, limit=101).status_code == 422
    assert mine(a, before=0).status_code == 422

    first = mine(a, limit=2).json()
    assert [item["approval_id"] for item in first["items"]] == created[::-1][:2]
    assert first["next_cursor"] == 2
    second = mine(a, limit=2, before=first["next_cursor"]).json()
    assert [item["approval_id"] for item in second["items"]] == [created[0]]
    assert second["next_cursor"] is None


def test_status_contract_rejects_incoherent_decisions():
    from generaldataagent.contracts import ApprovalStatus

    base = dict(
        approval_id=uuid4(),
        request_fingerprint="sha256:" + "a" * 64,
        task_id=uuid4(),
        tool_name="business.draft",
        action="deliver",
        resource="ticket",
        data_domains=("sales",),
        classification="internal",
        impact_scope="Create ticket",
        cost_estimate="One write",
    )
    now = datetime.now(UTC)
    window = {"created_at": now, "expires_at": now + timedelta(hours=1)}
    with pytest.raises(ValueError):
        ApprovalStatus(**base, **window, state="pending", decided_by="bob")
    with pytest.raises(ValueError):
        ApprovalStatus(**base, **window, state="approved")
    with pytest.raises(ValueError):
        ApprovalStatus(
            **base,
            **window,
            state="rejected",
            decided_at=now,
            decided_by="bob",
        )
    with pytest.raises(ValueError):
        ApprovalStatus(
            **base,
            **window,
            state="pending",
            decided_at=now,
            decided_by="bob",
            reason="why",
        )
    with pytest.raises(ValueError):
        ApprovalStatus(
            **base,
            created_at=now,
            expires_at=now,
            state="pending",
        )


def test_status_helper_keeps_grant_facts_after_disposition(authority):
    a = authority
    _, approval = create(a)
    approve(a, approval)
    a.store.cancel(
        UUID(approval["approval_id"]),
        requester_id="alice",
        reason="Requester changed plan",
    )
    item = approval_status(a.store.inspect(UUID(approval["approval_id"])), now=a.clock[0])
    assert item.state == "cancelled"
    assert item.decided_by == "alice"
    assert item.reason == "Requester changed plan"
