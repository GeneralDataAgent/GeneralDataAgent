"""Synthetic authenticated API -> metric graph -> persistent progress replay, without a model."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.task_events import TaskEventJournal
from generaldataagent.security.identity import SessionBroker
from generaldataagent.tools import SQLiteReadOnlyExecutor


def demonstrate(root: Path) -> dict:
    database = root / "orders.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE orders (created_at TEXT)")
        db.executemany("INSERT INTO orders VALUES (?)", [("2026-11-01",), ("2026-11-02",)])
    journal = TaskEventJournal(root / "events.db")
    runtime = MetricAnalysisRuntime(
        catalog=InMemoryMetricCatalog(
            [
                MetricDefinition(
                    name="order_count",
                    version="v1",
                    description="Synthetic order count",
                    data_domain="commerce",
                    relation="orders",
                    expression="COUNT(*)",
                    time_column="created_at",
                    owner="demo",
                )
            ]
        ),
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=LocalArtifactStore(root / "artifacts"),
        task_events=journal,
    )
    broker = SessionBroker()
    token = broker.issue(
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "x-user-id": "demo",
        "x-tenant-id": "demo",
        "x-project-id": "demo",
        "x-data-domain": "commerce",
    }
    with TestClient(create_app(runtime, session_broker=broker, task_events=journal)) as client:
        response = client.post(
            "/v1/analysis/metrics",
            headers=headers,
            json={"metric_name": "order_count", "metric_version": "v1"},
        )
        response.raise_for_status()
        task = response.json()
        events, cursor = [], 0
        while True:
            response = client.get(
                f"/v1/analysis/tasks/{task['task_id']}/events",
                headers=headers,
                params={"after": cursor, "limit": 2},
            )
            response.raise_for_status()
            page = response.json()
            events.extend(page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        return {"task_id": task["task_id"], "status": task["status"], "events": events}


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-task-events-") as directory:
        print(json.dumps(demonstrate(Path(directory)), ensure_ascii=False, indent=2))
