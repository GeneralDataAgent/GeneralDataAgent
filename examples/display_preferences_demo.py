"""Synthetic ASGI preference confirmation, state reopen and completed metric presentation."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from examples.task_submission_demo import demo_service
from generaldataagent.api.app import create_app
from generaldataagent.memory.display_preferences import ConfirmedDisplayPreferenceStore
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.task_presentation import MetricDisplayPreferenceService


def client_for(tasks, root):
    memory = ConfirmedDisplayPreferenceStore(
        root / "preferences.db",
        encryption_key=b"p" * 32,
        audit=JsonlAuditSink(root / "preference-audit.jsonl"),
    )
    token = tasks.broker.issue(
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "demo",
        "x-tenant-id": "demo",
        "x-project-id": "demo",
        "x-data-domain": "commerce",
        "idempotency-key": "prefs-demo",
    }
    app = create_app(
        session_broker=tasks.broker,
        task_events=tasks.events,
        metric_tasks=tasks,
        display_preferences=MetricDisplayPreferenceService(tasks=tasks, store=memory),
    )
    return TestClient(app, headers=headers)


def demonstrate(root):
    path = "/v1/analysis/preferences/display"
    with demo_service(root) as tasks, client_for(tasks, root) as client:
        saved = client.put(
            path,
            json={
                "expected_version": 0,
                "confirmed": True,
                "value": {"layout": "summary", "preview_rows": 1},
            },
        )
        saved.raise_for_status()
        accepted = client.post(
            "/v1/analysis/tasks", json={"metric_name": "order_count", "metric_version": "v1"}
        )
        accepted.raise_for_status()
        task_id = accepted.json()["task_id"]
        assert tasks.run_once().status == "completed"
        url = f"/v1/analysis/tasks/{task_id}/presentation"
        first = client.get(url)
        first.raise_for_status()
    with demo_service(root) as tasks, client_for(tasks, root) as client:
        factory = tasks.runtime_factory

        def no_execution(execution):
            runtime = factory(execution)

            def forbidden(*args, **kwargs):
                raise AssertionError("presentation must not execute SQL")

            runtime.executor.execute = forbidden
            return runtime

        tasks.runtime_factory = no_execution
        reopened = client.get(url)
        reopened.raise_for_status()
        assert reopened.json() == first.json()
        assert client.get(path).json() == saved.json()
        revoked = client.delete(path, params={"expected_version": 1})
        revoked.raise_for_status()
        default = client.get(url)
        default.raise_for_status()
        assert default.json()["preference"]["record"] is None
        assert client.get(path + "/versions/1").json() == first.json()["preference"]["record"]
        return {
            "asgi_api": True,
            "state_reopened": True,
            "confirmed_version": 1,
            "same_presentation_after_reopen": True,
            "first_layout": first.json()["layout"],
            "revoked_state": revoked.json()["status"],
            "default_layout": default.json()["layout"],
            "same_result_hash": first.json()["result_hash"] == default.json()["result_hash"],
            "model_calls": 0,
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-display-preference-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
