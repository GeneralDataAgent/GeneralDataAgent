import json
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

from generaldataagent.contracts import LineageAsset, LineageSnapshot
from generaldataagent.evaluation import CatalogLineageExport, load_external_gate_results

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "qualify_authoritative_catalog.py"


def _assets(*, dashboard_dependency: str = "metric.shop.revenue"):
    return (
        LineageAsset(asset_id="source.shop.orders", asset_type="source"),
        LineageAsset(
            asset_id="model.shop.orders_clean",
            asset_type="model",
            depends_on=("source.shop.orders",),
        ),
        LineageAsset(
            asset_id="metric.shop.revenue",
            asset_type="metric",
            depends_on=("model.shop.orders_clean",),
        ),
        LineageAsset(
            asset_id="dashboard.shop.sales",
            asset_type="dashboard",
            depends_on=(dashboard_dependency,),
        ),
    )


def _write_inputs(tmp_path: Path, *, dashboard_dependency: str = "metric.shop.revenue"):
    actual_path = tmp_path / "actual.json"
    authoritative_path = tmp_path / "authoritative.json"
    actual_path.write_text(
        LineageSnapshot(
            manifest_hash=f"sha256:{'1' * 64}",
            dbt_schema_version="v12",
            dbt_version="1.10.0",
            assets=_assets(),
            complete=True,
        ).model_dump_json()
    )
    authoritative_path.write_text(
        CatalogLineageExport(
            source_system="enterprise-catalog",
            assets=_assets(dashboard_dependency=dashboard_dependency),
            observed_at=datetime.now(UTC),
        ).model_dump_json()
    )
    return actual_path, authoritative_path


def test_authoritative_catalog_qualifier_emits_passing_evidence(tmp_path) -> None:
    actual, authoritative = _write_inputs(tmp_path)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_authoritative_catalog"]
    now = datetime.now(UTC)

    result = qualify(
        actual_snapshot_path=actual,
        authoritative_export_path=authoritative,
        actual_source_system="dbt-manifest",
        critical_assets=("source.shop.orders",),
        output_dir=output,
        verifier="catalog-reviewer",
        environment="staging",
        observed_at=now,
    )

    assert result["passed"] is True
    assert result["check_count"] == 3
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["authoritative_catalog"]["status"] == "passed"
    payload = json.loads((output / "authoritative_catalog.evidence.json").read_text())
    assert payload["checks"][2]["check"] == "dashboard_paths"
    assert str(tmp_path) not in json.dumps(payload)


def test_authoritative_catalog_qualifier_detects_dashboard_path_mismatch(tmp_path) -> None:
    actual, authoritative = _write_inputs(
        tmp_path,
        dashboard_dependency="model.shop.orders_clean",
    )
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_authoritative_catalog"]
    now = datetime.now(UTC)

    result = qualify(
        actual_snapshot_path=actual,
        authoritative_export_path=authoritative,
        actual_source_system="dbt-manifest",
        critical_assets=("source.shop.orders",),
        output_dir=output,
        verifier="catalog-reviewer",
        environment="non_production",
        observed_at=now,
    )

    assert result["passed"] is False
    assert result["failed_checks"] == 1
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["authoritative_catalog"]["status"] == "failed"
