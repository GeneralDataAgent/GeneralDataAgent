"""Run the Stage 2 read-only profiling, quality, and schema-diff foundation."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import duckdb

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import DuckDBConnector, SQLiteConnector
from generaldataagent.contracts import ActorContext, QualityRule
from generaldataagent.data_engineering import DataEngineeringRuntime


def main() -> None:
    with TemporaryDirectory(prefix="generaldataagent-stage2-") as directory:
        root = Path(directory)
        sqlite_path = root / "source.sqlite"
        duckdb_path = root / "source.duckdb"
        _create_sqlite(sqlite_path)
        _create_duckdb(duckdb_path)
        runtime = DataEngineeringRuntime(
            connectors={
                "operational": SQLiteConnector(sqlite_path, connector_id="operational"),
                "warehouse": DuckDBConnector(duckdb_path, connector_id="warehouse"),
            },
            connector_domains={
                "operational": frozenset({"commerce"}),
                "warehouse": frozenset({"commerce"}),
            },
            artifacts=LocalArtifactStore(root / "artifacts"),
        )
        actor = ActorContext(
            user_id="demo-user",
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domains=("commerce",),
        )
        task_id = uuid4()
        before = runtime.inspect_relation(
            task_id=task_id,
            actor=actor,
            connector_id="operational",
            relation="orders",
            trace_id="stage2-demo",
        )
        after = runtime.inspect_relation(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            relation="orders",
            trace_id="stage2-demo",
        )
        profile = runtime.profile_relation(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            relation="orders",
            trace_id="stage2-demo",
        )
        quality = runtime.check_quality(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            relation="orders",
            rules=(
                QualityRule(
                    rule_id="channel-required",
                    column="channel",
                    rule_type="not_null",
                ),
            ),
            trace_id="stage2-demo",
        )
        diff = runtime.diff_schemas(
            task_id=task_id,
            actor=actor,
            before=before,
            after=after,
            trace_id="stage2-demo",
        )

        print(f"profile rows={profile.row_count} columns={len(profile.columns)}")
        print(
            f"quality passed={quality.passed} "
            f"violations={quality.checks[0].violation_count}"
        )
        print(f"schema breaking={diff.breaking} changes={len(diff.changes)}")
        print(f"artifacts={len(tuple((root / 'artifacts').rglob('*.json')))}")


def _create_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", [(1, "app"), (2, None)])


def _create_duckdb(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE orders (id BIGINT NOT NULL, channel VARCHAR, amount DOUBLE)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(1, "app", 10.0), (2, None, 20.0)],
        )


if __name__ == "__main__":
    main()
