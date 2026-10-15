"""Qualify one real non-production dbt project and emit external-gate evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from generaldataagent.data_engineering import DbtCompileRunner
from generaldataagent.evaluation import ExternalGateEvidence, write_external_gate_manifest


def qualify_real_dbt_project(
    *,
    project_dir: Path,
    profiles_dir: Path,
    target: str,
    selected_resources: tuple[str, ...],
    output_dir: Path,
    verifier: str,
    source_system: str,
    environment: Literal["non_production", "staging"],
    executable: tuple[str, ...] = ("dbt",),
    timeout_seconds: float = 300.0,
    validity_hours: float = 24.0,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if not selected_resources:
        raise ValueError("real dbt qualification requires at least one selected resource")
    if environment not in {"non_production", "staging"}:
        raise ValueError("real dbt qualification cannot target production")
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "real_dbt_project.evidence.json"
    manifest_path = output_dir / "real_dbt_project.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("real dbt qualification evidence already exists")

    runner = DbtCompileRunner(
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        executable=executable,
        allowed_targets=frozenset({target}),
        timeout_seconds=timeout_seconds,
    )
    compile_report = runner.compile(
        target=target,
        selected_resources=selected_resources,
    )
    test_report = runner._test_selected(
        target=target,
        selected_resources=selected_resources,
    )
    qualified_at = observed_at or datetime.now(UTC)
    passed = compile_report.succeeded and test_report.succeeded
    payload = {
        "schema_version": "1.0",
        "gate_id": "real_dbt_project",
        "source_system": source_system,
        "environment": environment,
        "project_name": compile_report.project_name,
        "target": target,
        "selected_resources": selected_resources,
        "compile_report": compile_report.model_dump(mode="json"),
        "test_report": test_report.model_dump(mode="json"),
        "qualified_at": qualified_at.isoformat(),
        "passed": passed,
    }
    evidence_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(evidence_bytes)
    manifest = ExternalGateEvidence(
        gate_id="real_dbt_project",
        status="passed" if passed else "failed",
        source_system=source_system,
        environment=environment,
        verifier=verifier,
        summary=(
            f"dbt compile and selected tests {'passed' if passed else 'failed'} "
            f"for {compile_report.project_name}:{target}."
        ),
        evidence_path=evidence_path.name,
        evidence_hash=f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}",
        observed_at=qualified_at,
        valid_until=qualified_at + timedelta(hours=validity_hours),
    )
    write_external_gate_manifest(manifest_path, manifest)
    return {
        "passed": passed,
        "evidence_path": str(evidence_path),
        "manifest_path": str(manifest_path),
        "compile": compile_report.model_dump(mode="json"),
        "test": test_report.model_dump(mode="json"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--profiles-dir", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--selector", required=True, action="append")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verifier", required=True)
    parser.add_argument("--source-system", required=True)
    parser.add_argument(
        "--environment",
        choices=("non_production", "staging"),
        default="non_production",
    )
    parser.add_argument("--dbt-executable", default="dbt")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--validity-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = qualify_real_dbt_project(
        project_dir=args.project_dir,
        profiles_dir=args.profiles_dir,
        target=args.target,
        selected_resources=tuple(args.selector),
        output_dir=args.output_dir,
        verifier=args.verifier,
        source_system=args.source_system,
        environment=args.environment,
        executable=(args.dbt_executable,),
        timeout_seconds=args.timeout_seconds,
        validity_hours=args.validity_hours,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
