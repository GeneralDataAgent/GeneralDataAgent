import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.security.identity import SQLiteSessionBroker


@pytest.mark.parametrize(
    "mutation",
    [
        {"expires_at": "2030-01-01T00:00:00"},
        {"permissions": "operations:read"},
        {"permissions": ["not-a-permission"]},
        {"data_domains": "d"},
        {"data_domains": []},
        {"user_id": {"secret": "must-not-leak"}},
        {"tenant_id": " "},
        {"project_id": "p" * 129},
        {"unexpected": "must-not-leak"},
    ],
)
def test_malformed_durable_sessions_are_unauthorized_not_server_errors(tmp_path, mutation):
    path = tmp_path / "sessions"
    broker = SQLiteSessionBroker(path)
    token = broker.issue(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    with sqlite3.connect(path) as db:
        row = json.loads(db.execute("SELECT identity_json FROM sessions").fetchone()[0])
        row.update(mutation)
        db.execute("UPDATE sessions SET identity_json = ?", (json.dumps(row),))
    with pytest.raises(PermissionError, match="invalid_session_record"):
        broker.authenticate(token)
    response = TestClient(create_app(session_broker=broker)).get(
        "/v1/capabilities",
        headers={
            "authorization": f"Bearer {token}",
            "x-user-id": "u",
            "x-tenant-id": "t",
            "x-project-id": "p",
            "x-data-domain": "d",
        },
    )
    assert response.status_code == 401
    assert "must-not-leak" not in response.text


def test_identity_issuance_is_bounded_and_legacy_records_have_no_permissions(tmp_path):
    broker = SQLiteSessionBroker(tmp_path / "sessions")
    scope = dict(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    for changes in (
        {"user_id": "a\nb"},
        {"data_domains": "d"},
        {"data_domains": frozenset()},
        {"permissions": "operations:read"},
        {"permissions": frozenset({"admin:*"})},
        {"ttl_seconds": True},
        {"project_id": "p" * 129},
    ):
        with pytest.raises(ValueError):
            broker.issue(**{**scope, **changes})
    token = broker.issue(**scope)
    with sqlite3.connect(broker.path) as db:
        value = json.loads(db.execute("SELECT identity_json FROM sessions").fetchone()[0])
        value.pop("permissions")
        db.execute("UPDATE sessions SET identity_json = ?", (json.dumps(value),))
    assert broker.authenticate(token).permissions == frozenset()
    with pytest.raises(PermissionError):
        broker.authenticate("x" * 10000)
    with pytest.raises(PermissionError):
        broker.authenticate("not a bearer token")
