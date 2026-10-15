"""Validate a target-environment load transcript and emit gate evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from generaldataagent.evaluation import (
    ExternalGateEvidence,
    ProductionLoadTranscript,
    write_external_gate_manifest,
)


def qualify_production_scale_load(
    *,
    transcript_path: Path,
    output_dir: Path,
    verifier: str,
    minimum_request_count: int = 1_000,
    max_error_rate: float = 0.01,
    max_cancellation_rate: float = 0.01,
    validity_hours: float = 24.0,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if minimum_request_count < 1:
        raise ValueError("minimum_request_count must be positive")
    if not 0 <= max_error_rate <= 1:
        raise ValueError("max_error_rate must be between zero and one")
    if not 0 <= max_cancellation_rate <= 1:
        raise ValueError("max_cancellation_rate must be between zero and one")
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")

    transcript_bytes = transcript_path.resolve(strict=True).read_bytes()
    transcript = ProductionLoadTranscript.model_validate_json(transcript_bytes)
    error_rate = transcript.failed / transcript.request_count
    cancellation_rate = transcript.cancelled / transcript.request_count
    checks = {
        "production_scale": transcript.request_count >= minimum_request_count,
        "capacity_target": (
            transcript.observed_requests_per_second >= transcript.target_requests_per_second
        ),
        "outcome_accounting": (
            transcript.succeeded + transcript.failed + transcript.cancelled
            == transcript.request_count
        ),
        "error_budget": error_rate <= max_error_rate,
        "cancellation_budget": cancellation_rate <= max_cancellation_rate,
        "artifact_completeness": transcript.artifact_count == transcript.succeeded,
        "latency_budget": transcript.p95_request_ms <= transcript.max_p95_ms,
        "saturation_observed": transcript.saturation_observed,
        "backpressure_activated": transcript.backpressure_activated,
        "recovery_probe": transcript.recovery_probe_succeeded,
        "recovery_budget": transcript.recovery_seconds <= transcript.max_recovery_seconds,
    }
    passed = all(checks.values())

    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "production_scale_load.evidence.json"
    manifest_path = output_dir / "production_scale_load.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("production-scale load evidence already exists")
    qualified_at = observed_at or datetime.now(UTC)
    criteria = {
        "minimum_request_count": minimum_request_count,
        "max_error_rate": max_error_rate,
        "max_cancellation_rate": max_cancellation_rate,
        "target_requests_per_second": transcript.target_requests_per_second,
        "max_p95_ms": transcript.max_p95_ms,
        "max_recovery_seconds": transcript.max_recovery_seconds,
    }
    observations = {
        "request_count": transcript.request_count,
        "max_workers": transcript.max_workers,
        "succeeded": transcript.succeeded,
        "failed": transcript.failed,
        "cancelled": transcript.cancelled,
        "artifact_count": transcript.artifact_count,
        "observed_requests_per_second": transcript.observed_requests_per_second,
        "error_rate": error_rate,
        "cancellation_rate": cancellation_rate,
        "p50_request_ms": transcript.p50_request_ms,
        "p95_request_ms": transcript.p95_request_ms,
        "p99_request_ms": transcript.p99_request_ms,
        "saturation_signal": transcript.saturation_signal,
        "recovery_seconds": transcript.recovery_seconds,
    }
    payload = {
        "schema_version": "1.0",
        "gate_id": "production_scale_load",
        "source_system": transcript.source_system,
        "environment": transcript.environment,
        "run_id": transcript.run_id,
        "transcript_hash": f"sha256:{hashlib.sha256(transcript_bytes).hexdigest()}",
        "started_at": transcript.started_at.isoformat(),
        "completed_at": transcript.completed_at.isoformat(),
        "criteria": criteria,
        "observations": observations,
        "checks": checks,
        "qualified_at": qualified_at.isoformat(),
        "passed": passed,
    }
    evidence_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(evidence_bytes)
    failed_checks = [name for name, check_passed in checks.items() if not check_passed]
    manifest = ExternalGateEvidence(
        gate_id="production_scale_load",
        status="passed" if passed else "failed",
        source_system=transcript.source_system,
        environment=transcript.environment,
        verifier=verifier,
        summary=(
            "Production-scale load qualification passed."
            if passed
            else f"Production-scale load qualification failed: {', '.join(failed_checks)}."
        ),
        evidence_path=evidence_path.name,
        evidence_hash=f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}",
        observed_at=qualified_at,
        valid_until=qualified_at + timedelta(hours=validity_hours),
    )
    write_external_gate_manifest(manifest_path, manifest)
    return {
        "passed": passed,
        "checks": checks,
        "evidence_path": str(evidence_path),
        "manifest_path": str(manifest_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verifier", required=True)
    parser.add_argument("--minimum-request-count", type=int, default=1_000)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-cancellation-rate", type=float, default=0.01)
    parser.add_argument("--validity-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = qualify_production_scale_load(
        transcript_path=args.transcript,
        output_dir=args.output_dir,
        verifier=args.verifier,
        minimum_request_count=args.minimum_request_count,
        max_error_rate=args.max_error_rate,
        max_cancellation_rate=args.max_cancellation_rate,
        validity_hours=args.validity_hours,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
