from fastapi.testclient import TestClient

from generaldataagent.api.app import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_capabilities_do_not_claim_production_writes() -> None:
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    assert response.json()["production_writes"] is False
