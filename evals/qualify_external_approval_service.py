"""Validate an independent approval-service transcript and emit gate evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from generaldataagent.evaluation import (
    ExternalApprovalTranscript,
    ExternalGateEvidence,
    write_external_gate_manifest,
)


def qualify_external_approval_service(
    *,
    transcript_path: Path,
    output_dir: Path,
    verifier: str,
    validity_hours: float = 24.0,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")
    transcript_bytes = transcript_path.resolve(strict=True).read_bytes()
    transcript = ExternalApprovalTranscript.model_validate_json(transcript_bytes)
    checks = {
        "independent_approver": transcript.requester_id != transcript.approver_id,
        "request_bound": transcript.request_bound,
        "tenant_bound": transcript.tenant_bound,
        "self_approval_rejected": transcript.self_approval_rejected,
        "changed_request_rejected": transcript.changed_request_rejected,
        "replay_blocked": transcript.replay_blocked,
    }
    passed = all(checks.values())
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "external_approval_service.evidence.json"
    manifest_path = output_dir / "external_approval_service.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("external approval service evidence already exists")
    qualified_at = observed_at or datetime.now(UTC)
    payload = {
        "schema_version": "1.0",
        "gate_id": "external_approval_service",
        "source_system": transcript.source_system,
        "environment": transcript.environment,
        "service_request_id": transcript.service_request_id,
        "approval_id": str(transcript.approval_id),
        "request_fingerprint": transcript.request_fingerprint,
        "transcript_hash": f"sha256:{hashlib.sha256(transcript_bytes).hexdigest()}",
        "checks": checks,
        "requested_at": transcript.requested_at.isoformat(),
        "approved_at": transcript.approved_at.isoformat(),
        "consumed_at": transcript.consumed_at.isoformat(),
        "expires_at": transcript.expires_at.isoformat(),
        "qualified_at": qualified_at.isoformat(),
        "passed": passed,
    }
    evidence_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(evidence_bytes)
    failed_checks = [name for name, check_passed in checks.items() if not check_passed]
    manifest = ExternalGateEvidence(
        gate_id="external_approval_service",
        status="passed" if passed else "failed",
        source_system=transcript.source_system,
        environment=transcript.environment,
        verifier=verifier,
        summary=(
            "External approval controls passed."
            if passed
            else f"External approval controls failed: {', '.join(failed_checks)}."
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
    parser.add_argument("--validity-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = qualify_external_approval_service(
        transcript_path=args.transcript,
        output_dir=args.output_dir,
        verifier=args.verifier,
        validity_hours=args.validity_hours,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
