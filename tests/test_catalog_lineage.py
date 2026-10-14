import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    DbtManifestError,
    DbtManifestLineageLoader,
    analyze_impact,
)


def manifest_payload() -> dict:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.10.0",
        },
        "sources": {
            "source.shop.orders": {
                "unique_id": "source.shop.orders",
                "resource_type": "source",
                "depends_on": {"nodes": []},
            }
        },
        "nodes": {
            "model.shop.orders_clean": {
                "unique_id": "model.shop.orders_clean",
                "resource_type": "model",
                "depends_on": {"nodes": ["source.shop.orders", "test.shop.not_null"]},
            }
        },
        "metrics": {
            "metric.shop.revenue": {
                "unique_id": "metric.shop.revenue",
                "resource_type": "metric",
                "depends_on": {"nodes": ["model.shop.orders_clean"]},
            }
        },
        "exposures": {
            "exposure.shop.sales": {
                "unique_id": "exposure.shop.sales",
                "resource_type": "exposure",
                "depends_on": {"nodes": ["metric.shop.revenue"]},
            }
        },
    }


def write_manifest(path, payload=None) -> bytes:
    content = json.dumps(payload or manifest_payload(), sort_keys=True).encode()
    path.write_bytes(content)
    return content


def test_loads_complete_dbt_lineage_with_provenance(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    content = write_manifest(manifest)

    snapshot = DbtManifestLineageLoader(manifest).load()

    assert snapshot.complete is True
    assert snapshot.unresolved_dependencies == ()
    assert snapshot.dbt_version == "1.10.0"
    assert snapshot.manifest_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert [(asset.asset_id, asset.asset_type) for asset in snapshot.assets] == [
        ("exposure.shop.sales", "dashboard"),
        ("metric.shop.revenue", "metric"),
        ("model.shop.orders_clean", "model"),
        ("source.shop.orders", "source"),
    ]
    impact = analyze_impact(snapshot.assets, ("source.shop.orders",))
    assert [item.asset_id for item in impact.impacted_assets] == [
        "model.shop.orders_clean",
        "metric.shop.revenue",
        "exposure.shop.sales",
    ]


def test_marks_missing_supported_dependency_as_incomplete(tmp_path) -> None:
    payload = manifest_payload()
    payload["nodes"]["model.shop.orders_clean"]["depends_on"]["nodes"].append(
        "model.shop.missing"
    )
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, payload)

    snapshot = DbtManifestLineageLoader(manifest).load()

    assert snapshot.complete is False
    assert snapshot.unresolved_dependencies == ("model.shop.missing",)
    with pytest.raises(ValueError, match="unknown lineage dependencies"):
        analyze_impact(snapshot.assets, ("source.shop.orders",))


def test_rejects_manifest_over_size_limit(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)

    with pytest.raises(DbtManifestError, match="byte limit"):
        DbtManifestLineageLoader(manifest, max_bytes=10).load()


def test_runtime_policy_gates_and_persists_lineage_snapshot(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER)")
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        lineage_loader=DbtManifestLineageLoader(manifest),
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )

    result = runtime.ingest_dbt_lineage(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        trace_id="lineage-ingest",
    )

    assert result.complete is True
    assert len(result.assets) == 4
    assert list((tmp_path / "artifacts").rglob("*.json"))
