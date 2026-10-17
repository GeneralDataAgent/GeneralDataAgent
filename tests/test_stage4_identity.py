from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.security.identity import SessionBroker


def test_session_expiry_revocation_and_lifetime():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    broker = SessionBroker(clock=lambda: now)
    scope = dict(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    with pytest.raises(ValueError):
        broker.issue(**scope, ttl_seconds=901)
    token = broker.issue(**scope, ttl_seconds=1)
    assert broker.authenticate(token).tenant_id == "t"
    now += timedelta(seconds=1)
    with pytest.raises(PermissionError):
        broker.authenticate(token)
    token = broker.issue(**scope)
    broker.revoke(token)
    with pytest.raises(PermissionError):
        broker.authenticate(token)


def test_secured_api_rejects_forged_scope_before_execution():
    broker = SessionBroker()
    token = broker.issue(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    client = TestClient(create_app(session_broker=broker, production_mode=True))
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "u",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "d",
    }
    assert client.get("/health").status_code == 200
    assert client.get("/v1/capabilities").status_code == 401
    assert client.get("/v1/capabilities", headers=headers).status_code == 200
    for key in ("x-user-id", "x-tenant-id", "x-project-id", "x-data-domain"):
        assert client.get("/v1/capabilities", headers={**headers, key: "other"}).status_code == 403
    assert client.get("/v1/reviews/anything/summary", headers=headers).status_code == 403
    broker.revoke(token)
    assert client.get("/v1/capabilities", headers=headers).status_code == 401
    with pytest.raises(ValueError):
        create_app(production_mode=True)
