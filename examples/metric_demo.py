"""Run a local API with a synthetic certified metric and read-only SQLite data."""

import sqlite3
from pathlib import Path

import uvicorn

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def create_demo_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS orders")
        connection.execute("CREATE TABLE orders (created_at TEXT, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [
                ("2026-10-01T00:00:00+00:00", "web"),
                ("2026-10-02T00:00:00+00:00", "web"),
                ("2026-10-03T00:00:00+00:00", "store"),
            ],
        )


def build_demo_app():
    state_root = Path(".state/demo")
    database = state_root / "orders.db"
    create_demo_database(database)
    catalog = InMemoryMetricCatalog(
        [
            MetricDefinition(
                name="order_count",
                version="v1",
                description="Number of accepted orders",
                data_domain="commerce",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                allowed_dimensions=("channel",),
                owner="demo-data-team",
            )
        ]
    )
    runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database),
        artifacts=LocalArtifactStore(Path(".artifacts/demo")),
        audit=JsonlAuditSink(state_root / "audit.jsonl"),
    )
    return create_app(runtime)


if __name__ == "__main__":
    uvicorn.run(build_demo_app(), host="127.0.0.1", port=8000)
