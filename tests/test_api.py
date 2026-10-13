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


def test_metric_endpoint_is_fail_closed_when_not_configured() -> None:
    response = client.post(
        "/v1/analysis/metrics",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "commerce",
        },
        json={"metric_name": "order_count", "metric_version": "v1"},
    )
    assert response.status_code == 503
