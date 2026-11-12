"""Synthetic ASGI business-context confirmation, reopen and exact revocation."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from examples.task_submission_demo import demo_service
from generaldataagent.api.app import create_app
from generaldataagent.memory.business_context import ConfirmedBusinessContextStore
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.business_context import BusinessContextService


def confirmed_value(term, text):
    return {"entries": [{"kind": "business_definition", "term": term, "text": text}]}


def client_for(tasks, root):
    memory = ConfirmedBusinessContextStore(
        root / "context.db",
        encryption_key=b"b" * 32,
        audit=JsonlAuditSink(root / "context-audit.jsonl"),
        certified_terms=lambda: ("order_count",),
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
        "idempotency-key": "context-demo",
    }
    app = create_app(
        session_broker=tasks.broker,
        task_events=tasks.events,
        metric_tasks=tasks,
        business_context=BusinessContextService(tasks=tasks, store=memory),
    )
    return TestClient(app, headers=headers)


def demonstrate(root):
    path = "/v1/analysis/context"
    body = {
        "expected_version": 0,
        "confirmed": True,
        "value": {
            "entries": [
                {"kind": "business_definition", "term": "GMV", "text": "含税成交总额"},
                {"kind": "project_context", "term": "Q4", "text": "本季度只看自营渠道"},
            ]
        },
    }
    with demo_service(root) as tasks, client_for(tasks, root) as client:
        refused = client.put(
            path,
            json={
                "expected_version": 0,
                "confirmed": True,
                "value": confirmed_value("order_count", "自定义口径"),
            },
        )
        assert refused.status_code == 409
        saved = client.put(path, json=body)
        saved.raise_for_status()
    with demo_service(root) as tasks, client_for(tasks, root) as client:
        reopened = client.get(path)
        reopened.raise_for_status()
        assert reopened.json() == saved.json()
        assert tasks.run_once() is None  # Stored context never starts work by itself.
        revoked = client.delete(path, params={"expected_version": 1})
        revoked.raise_for_status()
        after = client.get(path)
        after.raise_for_status()
        history = client.get(path + "/versions/1")
        history.raise_for_status()
        assert history.json() == saved.json()["record"]
        return {
            "asgi_api": True,
            "state_reopened": True,
            "confirmed_version": saved.json()["version"],
            "same_state_after_reopen": True,
            "entry_count": len(saved.json()["record"]["value"]["entries"]),
            "certified_metric_redefinition_refused": refused.json()["detail"],
            "revoked_state": revoked.json()["status"],
            "active_record_after_revoke": after.json()["record"],
            "history_citation_available": history.json()["version"] == 1,
            "model_calls": 0,
            "sql_executions": 0,
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-business-context-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
