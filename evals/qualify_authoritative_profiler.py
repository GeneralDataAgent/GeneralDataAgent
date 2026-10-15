"""Compare generated and authoritative profiles and emit external-gate evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from generaldataagent.contracts import DataProfile
from generaldataagent.data_engineering import compare_profiles
from generaldataagent.evaluation import ExternalGateEvidence, write_external_gate_manifest


def qualify_authoritative_profiler(
    *,
    actual_profile_path: Path,
    authoritative_profile_path: Path,
    actual_source_system: str,
    authoritative_source_system: str,
    output_dir: Path,
    verifier: str,
    environment: Literal["non_production", "staging", "production"],
    threshold: float = 0.99,
    numeric_tolerance: float = 1e-9,
    validity_hours: float = 24.0,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if not actual_source_system or not authoritative_source_system:
        raise ValueError("profile source systems must not be empty")
    if actual_source_system.casefold() == authoritative_source_system.casefold():
        raise ValueError("authoritative profile must come from an independent source system")
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")
    actual_bytes = actual_profile_path.resolve(strict=True).read_bytes()
    authoritative_bytes = authoritative_profile_path.resolve(strict=True).read_bytes()
    actual = DataProfile.model_validate_json(actual_bytes)
    authoritative = DataProfile.model_validate_json(authoritative_bytes)
    comparison = compare_profiles(
        actual,
        authoritative,
        numeric_tolerance=numeric_tolerance,
        threshold=threshold,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "authoritative_profiler.evidence.json"
    manifest_path = output_dir / "authoritative_profiler.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("authoritative profiler evidence already exists")
    qualified_at = observed_at or datetime.now(UTC)
    payload = {
        "schema_version": "1.0",
        "gate_id": "authoritative_profiler",
        "actual_source_system": actual_source_system,
        "authoritative_source_system": authoritative_source_system,
        "environment": environment,
        "relation": actual.relation,
        "actual_profile_hash": f"sha256:{hashlib.sha256(actual_bytes).hexdigest()}",
        "authoritative_profile_hash": (
            f"sha256:{hashlib.sha256(authoritative_bytes).hexdigest()}"
        ),
        "comparison": comparison.model_dump(mode="json"),
        "qualified_at": qualified_at.isoformat(),
        "passed": comparison.passed,
    }
    evidence_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(evidence_bytes)
    manifest = ExternalGateEvidence(
        gate_id="authoritative_profiler",
        status="passed" if comparison.passed else "failed",
        source_system=authoritative_source_system,
        environment=environment,
        verifier=verifier,
        summary=(
            f"Profile agreement for {actual.relation} was "
            f"{comparison.agreement_rate:.6f} at threshold {comparison.threshold:.6f}."
        ),
        evidence_path=evidence_path.name,
        evidence_hash=f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}",
        observed_at=qualified_at,
        valid_until=qualified_at + timedelta(hours=validity_hours),
    )
    write_external_gate_manifest(manifest_path, manifest)
    return {
        "passed": comparison.passed,
        "agreement_rate": comparison.agreement_rate,
        "threshold": comparison.threshold,
        "evidence_path": str(evidence_path),
        "manifest_path": str(manifest_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--actual-profile", required=True, type=Path)
    parser.add_argument("--authoritative-profile", required=True, type=Path)
    parser.add_argument("--actual-source-system", required=True)
    parser.add_argument("--authoritative-source-system", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verifier", required=True)
    parser.add_argument(
        "--environment",
        choices=("non_production", "staging", "production"),
        default="non_production",
    )
    parser.add_argument("--threshold", type=float, default=0.99)
    parser.add_argument("--numeric-tolerance", type=float, default=1e-9)
    parser.add_argument("--validity-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = qualify_authoritative_profiler(
        actual_profile_path=args.actual_profile,
        authoritative_profile_path=args.authoritative_profile,
        actual_source_system=args.actual_source_system,
        authoritative_source_system=args.authoritative_source_system,
        output_dir=args.output_dir,
        verifier=args.verifier,
        environment=args.environment,
        threshold=args.threshold,
        numeric_tolerance=args.numeric_tolerance,
        validity_hours=args.validity_hours,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
