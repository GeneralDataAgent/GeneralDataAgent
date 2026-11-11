"""Synthetic API acceptance, process exit, separate worker, and authenticated replay."""

import atexit
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import ActorContext
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.orchestration.task_events import TaskEventJournal
from generaldataagent.orchestration.task_queue import MetricTaskQueue
from generaldataagent.orchestration.task_service import MetricTaskService
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.tools.sqlite_guarded import GuardedSQLiteReadOnlyExecutor

_WORKER_STACK = ExitStack()
atexit.register(_WORKER_STACK.close)


@contextmanager
def demo_service(root: Path, *, feedback=None):
    root.mkdir(parents=True, exist_ok=True)
    database = root / "orders.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS orders (created_at TEXT)")
        if not db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
            db.executemany("INSERT INTO orders VALUES (?)", [("2026-11-01",), ("2026-11-02",)])
    events = TaskEventJournal(root / "events.db")
    budgets = TaskBudgetLedger(root / "budgets.db")
    broker = SQLiteSessionBroker(root / "sessions.db")
    # Public disposable fixture keys, exclusively for this synthetic demonstration.
    queue = MetricTaskQueue(root / "jobs.db", encryption_key=b"q" * 32)
    artifacts = LocalArtifactStore(root / "artifacts")
    audit = JsonlAuditSink(root / "audit.jsonl")
    catalog = InMemoryMetricCatalog(
        [
            MetricDefinition(
                name="order_count",
                version="v1",
                description="Synthetic orders",
                aliases=("订单数", "订单量", "orders"),
                data_domain="commerce",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                allowed_dimensions=("created_at",),
                owner="demo",
            )
        ]
    )
    executor = GuardedSQLiteReadOnlyExecutor(database)
    with open_protected_checkpointer(root / "checkpoints.db", encryption_key=b"c" * 32) as saver:

        def factory(execution):
            claim = execution.claim
            return MetricAnalysisRuntime(
                catalog=catalog,
                executor=executor,
                artifacts=artifacts,
                checkpointer=saver,
                task_events=events,
                execution_guard=execution.guard,
                composition=ExecutionComposition(
                    "research",
                    claim.actor,
                    artifacts,
                    audit,
                    budgets,
                    claim.record.task_id,
                    str(claim.record.task_id),
                ),
            )

        yield MetricTaskService(
            queue=queue,
            broker=broker,
            events=events,
            budgets=budgets,
            runtime_factory=factory,
            feedback=feedback,
            question_catalog=catalog,
        )


def worker_factory():
    """Trusted synthetic factory for --factory examples.task_submission_demo:worker_factory."""
    root = Path(os.environ["GDA_TASK_DEMO_ROOT"])
    feedback = None
    if path := os.environ.get("GDA_TASK_DEMO_FEEDBACK"):
        from generaldataagent.business.governance import FeedbackRegistry

        feedback = FeedbackRegistry(Path(path))
    return _WORKER_STACK.enter_context(demo_service(root, feedback=feedback))


def demonstrate(root: Path) -> dict:
    owner = ActorContext(
        user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
    )
    with demo_service(root) as service:
        token = service.broker.issue(
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
            "idempotency-key": "demo-1",
        }
        with TestClient(
            create_app(
                session_broker=service.broker, task_events=service.events, metric_tasks=service
            )
        ) as client:
            response = client.post(
                "/v1/analysis/tasks",
                headers=headers,
                json={"metric_name": "order_count", "metric_version": "v1"},
            )
            response.raise_for_status()
            accepted = response.json()
            assert accepted["status"] == "queued"
            assert not list((root / "artifacts").rglob("*.json"))
    worker = subprocess.run(
        [
            sys.executable,
            "-m",
            "generaldataagent.services.metric_task_worker",
            "--development",
            "--once",
            "--factory",
            "examples.task_submission_demo:worker_factory",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ | {"GDA_TASK_DEMO_ROOT": str(root.resolve())},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    with demo_service(root) as reopened:
        record = reopened.read(accepted["task_id"], owner, token)
        events = reopened.events.read(record.task_id, owner)
        with TestClient(
            create_app(
                session_broker=reopened.broker, task_events=reopened.events, metric_tasks=reopened
            )
        ) as client:
            listed = client.get("/v1/analysis/tasks", headers=headers)
            listed.raise_for_status()
            result = client.get(f"/v1/analysis/tasks/{record.task_id}/result", headers=headers)
            result.raise_for_status()
        return {
            "accepted_status": accepted["status"],
            "status": record.status,
            "task_id": str(record.task_id),
            "thread_id": str(record.thread_id),
            "attempts": record.attempts,
            "events": len(events.events),
            "worker": json.loads(worker.stdout),
            "model_calls": 0,
            "listed_tasks": len(listed.json()["tasks"]),
            "result_rows": result.json()["delivery"]["rows"],
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-task-submission-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
