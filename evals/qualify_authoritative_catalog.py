"""Compare dbt lineage with an authoritative catalog and emit gate evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from generaldataagent.contracts import LineageSnapshot
from generaldataagent.data_engineering import analyze_impact
from generaldataagent.evaluation import (
    CatalogLineageExport,
    ExternalGateEvidence,
    write_external_gate_manifest,
)


def qualify_authoritative_catalog(
    *,
    actual_snapshot_path: Path,
    authoritative_export_path: Path,
    actual_source_system: str,
    critical_assets: tuple[str, ...],
    output_dir: Path,
    verifier: str,
    environment: Literal["non_production", "staging", "production"],
    validity_hours: float = 24.0,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if not actual_source_system:
        raise ValueError("actual lineage source system must not be empty")
    if not critical_assets:
        raise ValueError("catalog qualification requires at least one critical asset")
    if len(set(critical_assets)) != len(critical_assets):
        raise ValueError("critical assets must be unique")
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")
    actual_bytes = actual_snapshot_path.resolve(strict=True).read_bytes()
    authoritative_bytes = authoritative_export_path.resolve(strict=True).read_bytes()
    actual = LineageSnapshot.model_validate_json(actual_bytes)
    authoritative = CatalogLineageExport.model_validate_json(authoritative_bytes)
    if not actual.complete:
        raise ValueError("actual lineage snapshot must be complete")
    if actual_source_system.casefold() == authoritative.source_system.casefold():
        raise ValueError("authoritative catalog must be an independent source system")

    actual_assets = {asset.asset_id: asset for asset in actual.assets}
    authoritative_assets = {asset.asset_id: asset for asset in authoritative.assets}
    missing_actual = set(critical_assets) - actual_assets.keys()
    missing_authoritative = set(critical_assets) - authoritative_assets.keys()
    if missing_actual:
        raise ValueError(f"critical assets missing from actual lineage: {sorted(missing_actual)}")
    if missing_authoritative:
        raise ValueError(
            f"critical assets missing from authoritative catalog: {sorted(missing_authoritative)}"
        )

    checks: list[dict[str, Any]] = []
    for asset_id in critical_assets:
        actual_asset = actual_assets[asset_id]
        authoritative_asset = authoritative_assets[asset_id]
        checks.append(
            {
                "asset_id": asset_id,
                "check": "asset_type",
                "passed": actual_asset.asset_type == authoritative_asset.asset_type,
                "actual": actual_asset.asset_type,
                "authoritative": authoritative_asset.asset_type,
            }
        )
        checks.append(
            {
                "asset_id": asset_id,
                "check": "direct_dependencies",
                "passed": set(actual_asset.depends_on) == set(authoritative_asset.depends_on),
                "actual": sorted(actual_asset.depends_on),
                "authoritative": sorted(authoritative_asset.depends_on),
            }
        )
        actual_dashboards = _dashboard_paths(actual.assets, asset_id)
        authoritative_dashboards = _dashboard_paths(authoritative.assets, asset_id)
        checks.append(
            {
                "asset_id": asset_id,
                "check": "dashboard_paths",
                "passed": actual_dashboards == authoritative_dashboards,
                "actual": actual_dashboards,
                "authoritative": authoritative_dashboards,
            }
        )

    passed = all(check["passed"] for check in checks)
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "authoritative_catalog.evidence.json"
    manifest_path = output_dir / "authoritative_catalog.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("authoritative catalog evidence already exists")
    qualified_at = observed_at or datetime.now(UTC)
    payload = {
        "schema_version": "1.0",
        "gate_id": "authoritative_catalog",
        "actual_source_system": actual_source_system,
        "authoritative_source_system": authoritative.source_system,
        "environment": environment,
        "critical_assets": critical_assets,
        "actual_snapshot_hash": f"sha256:{hashlib.sha256(actual_bytes).hexdigest()}",
        "authoritative_export_hash": (f"sha256:{hashlib.sha256(authoritative_bytes).hexdigest()}"),
        "checks": checks,
        "qualified_at": qualified_at.isoformat(),
        "passed": passed,
    }
    evidence_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(evidence_bytes)
    failed_checks = sum(not check["passed"] for check in checks)
    manifest = ExternalGateEvidence(
        gate_id="authoritative_catalog",
        status="passed" if passed else "failed",
        source_system=authoritative.source_system,
        environment=environment,
        verifier=verifier,
        summary=(
            f"Compared {len(critical_assets)} critical assets and Dashboard paths; "
            f"{failed_checks} of {len(checks)} checks failed."
        ),
        evidence_path=evidence_path.name,
        evidence_hash=f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}",
        observed_at=qualified_at,
        valid_until=qualified_at + timedelta(hours=validity_hours),
    )
    write_external_gate_manifest(manifest_path, manifest)
    return {
        "passed": passed,
        "check_count": len(checks),
        "failed_checks": failed_checks,
        "evidence_path": str(evidence_path),
        "manifest_path": str(manifest_path),
    }


def _dashboard_paths(assets, changed_asset: str) -> list[dict[str, Any]]:
    impact = analyze_impact(assets, (changed_asset,))
    return [
        {"asset_id": item.asset_id, "path": item.path}
        for item in impact.impacted_assets
        if item.asset_type == "dashboard"
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--actual-snapshot", required=True, type=Path)
    parser.add_argument("--authoritative-export", required=True, type=Path)
    parser.add_argument("--actual-source-system", required=True)
    parser.add_argument("--critical-asset", required=True, action="append")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verifier", required=True)
    parser.add_argument(
        "--environment",
        choices=("non_production", "staging", "production"),
        default="non_production",
    )
    parser.add_argument("--validity-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = qualify_authoritative_catalog(
        actual_snapshot_path=args.actual_snapshot,
        authoritative_export_path=args.authoritative_export,
        actual_source_system=args.actual_source_system,
        critical_assets=tuple(args.critical_asset),
        output_dir=args.output_dir,
        verifier=args.verifier,
        environment=args.environment,
        validity_hours=args.validity_hours,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
