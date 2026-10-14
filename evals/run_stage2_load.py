"""Run the reproducible local Stage 2 profiling load gate."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext
from generaldataagent.data_engineering import DataEngineeringRuntime, run_profile_load_gate


def main() -> int:
    with TemporaryDirectory(prefix="generaldataagent-load-") as temporary_directory:
        root = Path(temporary_directory)
        database = root / "load.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT, amount REAL)"
            )
            connection.executemany(
                "INSERT INTO orders VALUES (?, ?, ?)",
                [(index, "app" if index % 2 else "web", float(index)) for index in range(1_000)],
            )
        runtime = DataEngineeringRuntime(
            connectors={
                "warehouse": SQLiteConnector(database, connector_id="warehouse"),
            },
            connector_domains={"warehouse": frozenset({"commerce"})},
            artifacts=LocalArtifactStore(root / "artifacts"),
        )
        result = run_profile_load_gate(
            runtime,
            actor=ActorContext(
                user_id="stage2-load-gate",
                tenant_id="local",
                project_id="stage2",
                data_domains=("commerce",),
            ),
            connector_id="warehouse",
            relation="orders",
            request_count=100,
            max_workers=8,
            max_p95_ms=5_000,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
