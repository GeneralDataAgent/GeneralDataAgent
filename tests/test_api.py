from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from generaldataagent.api.app import app, create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ExperimentSpec, SnapshotColumn
from generaldataagent.data_science import DataScienceWorkbench, create_dataset_snapshot

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


def test_question_endpoint_is_fail_closed_when_not_configured() -> None:
    response = client.post(
        "/v1/analysis/questions",
        headers={
            "x-user-id": "user-1",
            "x-tenant-id": "tenant-1",
            "x-project-id": "project-1",
            "x-data-domain": "commerce",
        },
        json={"question": "How many orders?"},
    )
    assert response.status_code == 503


def test_science_endpoint_runs_only_configured_fixed_workbench(tmp_path: Path) -> None:
    task_id = uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts")
    rows = [{"feature": index, "target": "yes" if index % 3 == 0 else "no"} for index in range(20)]
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id="tenant-1",
        project_id="project-1",
        data_domain="commerce",
        source_name="api-test",
        rows=rows,
        columns=(
            SnapshotColumn(name="feature", data_type="integer", role="feature"),
            SnapshotColumn(name="target", data_type="string", role="target"),
        ),
        artifact_store=store,
    )
    experiment = ExperimentSpec(
        task_id=task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type="binary_classification",
        target_column="target",
        feature_columns=("feature",),
        baseline="majority",
        primary_metric="accuracy",
    )
    headers = {
        "x-user-id": "scientist",
        "x-tenant-id": "tenant-1",
        "x-project-id": "project-1",
        "x-data-domain": "commerce",
        "x-trace-id": "science-api-test",
    }
    payload = {
        "snapshot": snapshot.model_dump(mode="json"),
        "experiment": experiment.model_dump(mode="json"),
    }
    assert client.post("/v1/science/experiments", headers=headers, json=payload).status_code == 503
    configured = TestClient(
        create_app(science_workbench=DataScienceWorkbench(artifact_store=store))
    )
    response = configured.post(
        "/v1/science/experiments",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True
    assert response.json()["experiment_id"] == str(experiment.experiment_id)
    wrong_tenant = configured.post(
        "/v1/science/experiments",
        headers=headers | {"x-tenant-id": "tenant-2"},
        json=payload,
    )
    assert wrong_tenant.status_code == 403
