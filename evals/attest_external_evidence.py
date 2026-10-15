"""Sign an external gate manifest with an Ed25519 verifier key."""

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from generaldataagent.evaluation import (
    ExternalEvidenceAttestation,
    ExternalEvidenceAttestationStatement,
    ExternalGateEvidence,
    serialize_attestation_statement,
)


def attest_external_evidence(
    *,
    manifest_path: Path,
    private_key_path: Path,
    key_id: str,
    signed_at: datetime | None = None,
) -> dict[str, Any]:
    resolved_manifest = manifest_path.resolve(strict=True)
    manifest_bytes = resolved_manifest.read_bytes()
    manifest = ExternalGateEvidence.model_validate_json(manifest_bytes)
    expected_name = f"{manifest.gate_id}.manifest.json"
    if resolved_manifest.name != expected_name:
        raise ValueError(f"external manifest must be named {expected_name}")
    evidence_path = (resolved_manifest.parent / manifest.evidence_path).resolve(strict=True)
    if not evidence_path.is_relative_to(resolved_manifest.parent) or not evidence_path.is_file():
        raise ValueError("external evidence file must stay inside its bundle directory")
    evidence_hash = f"sha256:{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}"
    if evidence_hash != manifest.evidence_hash:
        raise ValueError("external evidence hash mismatch")

    private_key_bytes = private_key_path.resolve(strict=True).read_bytes()
    private_key = load_pem_private_key(private_key_bytes, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("external evidence signing key must be an Ed25519 private key")
    actual_signed_at = signed_at or datetime.now(UTC)
    statement = ExternalEvidenceAttestationStatement(
        gate_id=manifest.gate_id,
        verifier=manifest.verifier,
        key_id=key_id,
        manifest_hash=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        signed_at=actual_signed_at,
    )
    signature = private_key.sign(serialize_attestation_statement(statement))
    attestation = ExternalEvidenceAttestation(
        **statement.model_dump(),
        signature=f"base64:{base64.b64encode(signature).decode()}",
    )
    output_path = resolved_manifest.with_name(f"{manifest.gate_id}.attestation.json")
    if output_path.exists():
        raise FileExistsError("external evidence attestation already exists")
    output_path.write_text(
        json.dumps(attestation.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return {
        "gate_id": manifest.gate_id,
        "verifier": manifest.verifier,
        "key_id": key_id,
        "signed_at": actual_signed_at.isoformat(),
        "attestation_path": str(output_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args()
    result = attest_external_evidence(
        manifest_path=args.manifest,
        private_key_path=args.private_key,
        key_id=args.key_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
