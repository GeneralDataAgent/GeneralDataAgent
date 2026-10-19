from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_stage5_actions import PAYLOADS, intent
from test_stage5_actions import setup as base_setup

from generaldataagent.api.app import create_app
from generaldataagent.business.actions import ActionControl


@pytest.fixture
def setup(tmp_path):
    return base_setup.__wrapped__(tmp_path)


def test_console_shell_is_secret_free_and_browser_isolation_headers(setup):
    runtime, _, broker, token = setup
    client = TestClient(create_app(business_actions=runtime, session_broker=broker))
    for path in ["/business", "/business/console.js", "/business/console.css"]:
        response = client.get(path)
        assert response.status_code == 200
        assert token not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/business/session").status_code == 401
    assert client.get("/business/session", headers={"authorization": f"Bearer {token}"}).json() == {
        "user_id": "alice",
        "tenant_id": "t",
        "project_id": "p",
        "data_domains": ["sales"],
        "permissions": ["actions:resume", "business:propose", "business:read"],
    }
    broker.revoke(token)
    assert (
        client.get("/business/session", headers={"authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_scoped_keyset_pages_do_not_skip_or_expose_other_actors(setup):
    runtime, _, broker, token = setup
    actions = [intent(PAYLOADS[1]) for _ in range(5)]
    for action in actions:
        runtime.propose(action, token)
    other = broker.issue(
        user_id="other",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read"}),
    )
    runtime.propose(intent(PAYLOADS[1]), other)
    page = runtime.list(token, limit=2)
    ids = [v["intent"]["action_id"] for v in page["items"]]
    while page["next_cursor"] is not None:
        page = runtime.list(token, limit=2, before=page["next_cursor"])
        ids.extend(v["intent"]["action_id"] for v in page["items"])
    assert ids == [str(a.action_id) for a in reversed(actions)]
    assert len(runtime.list(other)["items"]) == 1
    for bad in [0, -1, 101, True]:
        with pytest.raises(ValueError):
            runtime.list(token, limit=bad)
    with pytest.raises(ValueError):
        runtime.list(token, before=0)


def test_domain_filter_is_applied_before_pagination(setup):
    runtime, _, broker, token = setup
    public = intent(PAYLOADS[1])
    runtime.propose(public, token)
    runtime.controls[("dashboard", "dashboard")] = ActionControl(
        "dashboard", "dashboard", "ops", "t", "p", frozenset({"restricted"}), enabled=True
    )
    broad = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales", "restricted"}),
        permissions=frozenset({"business:propose"}),
    )
    runtime.propose(intent(PAYLOADS[0]), broad)
    page = runtime.list(token, limit=1)
    assert [v["intent"]["action_id"] for v in page["items"]] == [str(public.action_id)]
    assert page["next_cursor"] is None


def test_http_list_and_control_keep_exact_approval(setup):
    runtime, approvals, broker, token = setup
    action = intent(PAYLOADS[1])
    view = runtime.propose(action, token)
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "alice",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "sales",
    }
    client = TestClient(create_app(business_actions=runtime, session_broker=broker))
    assert client.get("/v1/business/drafts", headers=headers).json()["items"] == [view]
    assert (
        client.post(f"/v1/business/drafts/{action.action_id}/execute", headers=headers).status_code
        == 403
    )
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    assert (
        client.post(f"/v1/business/drafts/{action.action_id}/execute", headers=headers).status_code
        == 200
    )
    assert client.get("/v1/business/drafts?limit=101", headers=headers).status_code == 422
    assert client.get("/v1/business/drafts").status_code == 401
    assert (
        client.post(f"/v1/business/drafts/{action.action_id}/approve", headers=headers).status_code
        == 404
    )


def test_console_session_denies_non_readers_and_service_shutdown(setup):
    runtime, _, broker, _ = setup
    token = broker.issue(
        user_id="operator",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose"}),
    )
    client = TestClient(create_app(business_actions=runtime, session_broker=broker))
    assert (
        client.get("/business/session", headers={"authorization": f"Bearer {token}"}).status_code
        == 401
    )
    runtime.service_enabled = lambda: False
    assert (
        client.get("/business/session", headers={"authorization": f"Bearer {token}"}).status_code
        == 503
    )
