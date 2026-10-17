from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.observability.slo import ServiceSLO
from generaldataagent.security.identity import SQLiteSessionBroker


def test_authenticated_users_cannot_escalate_to_other_service_roles(tmp_path):
    broker = SQLiteSessionBroker(tmp_path / "sessions")
    api = TestClient(create_app(session_broker=broker, slo=ServiceSLO(), production_mode=True))
    scope = dict(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))

    def headers(permissions):
        token = broker.issue(**scope, permissions=frozenset(permissions))
        # Permissions must survive loading from another broker/process.
        assert SQLiteSessionBroker(tmp_path / "sessions").authenticate(
            token
        ).permissions == frozenset(permissions)
        return {
            "authorization": f"Bearer {token}",
            "x-user-id": "u",
            "x-tenant-id": "t",
            "x-project-id": "p",
            "x-data-domain": "d",
            "x-permissions": "operations:read",
        }

    ordinary = headers([])
    assert api.get("/v1/capabilities", headers=ordinary).status_code == 200
    assert api.get("/v1/operations/slo", headers=ordinary).status_code == 403
    assert api.post("/v1/analysis/metrics", headers=ordinary, json={}).status_code == 403
    assert api.post("/v1/science/experiments", headers=ordinary, json={}).status_code == 403
    assert api.post("/v1/actions/thread/resume", headers=ordinary).status_code == 403
    assert api.get("/v1/operations/slo", headers=headers(["analysis:run"])).status_code == 403
    assert api.get("/v1/operations/slo", headers=headers(["operations:read"])).status_code == 200
