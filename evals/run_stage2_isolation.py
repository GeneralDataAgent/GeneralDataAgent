"""Run 50 concurrent profiling groups to detect task or artifact cross-talk."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext, ProfileBatchRequest
from generaldataagent.data_engineering import DataEngineeringRuntime


def run_isolation_evaluation(*, group_count: int = 50) -> dict[str, Any]:
    if group_count < 1 or group_count > 500:
        raise ValueError("group_count must be between 1 and 500")
    with TemporaryDirectory(prefix="generaldataagent-isolation-") as directory:
        root = Path(directory)
        small = root / "small.sqlite"
        large = root / "large.sqlite"
        _create_database(small, 3)
        _create_database(large, 7)
        artifacts = LocalArtifactStore(root / "artifacts")
        runtime = DataEngineeringRuntime(
            connectors={
                "small": SQLiteConnector(small, connector_id="small"),
                "large": SQLiteConnector(large, connector_id="large"),
            },
            connector_domains={
                "small": frozenset({"commerce"}),
                "large": frozenset({"commerce"}),
            },
            artifacts=artifacts,
        )
        actor = ActorContext(
            user_id="stage2-isolation-gate",
            tenant_id="local",
            project_id="stage2",
            data_domains=("commerce",),
        )

        def run_group(index: int) -> dict[str, Any]:
            requests = (
                ProfileBatchRequest(task_id=uuid4(), connector_id="small", relation="orders"),
                ProfileBatchRequest(task_id=uuid4(), connector_id="large", relation="orders"),
            )
            batch = runtime.profile_relations_parallel(
                actor=actor,
                requests=requests,
                trace_id=f"stage2-isolation:{index}",
                max_workers=2,
            )
            return {
                "group": index,
                "statuses": [item.status for item in batch.items],
                "row_counts": [
                    item.profile.row_count if item.profile else None for item in batch.items
                ],
                "task_ids": [str(item.task_id) for item in batch.items],
            }

        started_at = monotonic()
        with ThreadPoolExecutor(max_workers=8) as executor:
            groups = list(executor.map(run_group, range(group_count)))
        elapsed_ms = (monotonic() - started_at) * 1_000
        task_ids = [task_id for group in groups for task_id in group["task_ids"]]
        failures = [
            {
                "group": group["group"],
                "statuses": group["statuses"],
                "row_counts": group["row_counts"],
            }
            for group in groups
            if group["statuses"] != ["succeeded", "succeeded"]
            or group["row_counts"] != [3, 7]
        ]
        artifact_count = len(tuple(artifacts.root.rglob("*.json")))
        unique_task_ids = len(set(task_ids))
        passed = (
            not failures
            and len(task_ids) == group_count * 2
            and unique_task_ids == group_count * 2
            and artifact_count == group_count * 2
        )
        return {
            "group_count": group_count,
            "request_count": group_count * 2,
            "failed_groups": len(failures),
            "artifact_count": artifact_count,
            "unique_task_ids": unique_task_ids,
            "elapsed_ms": elapsed_ms,
            "passed": passed,
            "failures": failures,
        }


def _create_database(path: Path, row_count: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER NOT NULL)")
        connection.executemany(
            "INSERT INTO orders VALUES (?)", [(index,) for index in range(row_count)]
        )


if __name__ == "__main__":
    result = run_isolation_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
