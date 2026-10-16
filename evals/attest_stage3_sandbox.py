"""Sign a Stage 3 hardened-sandbox manifest with an Ed25519 reviewer key."""

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from generaldataagent.evaluation import (
    SandboxReviewAttestation,
    SandboxReviewAttestationStatement,
    StageThreeGateManifest,
    serialize_sandbox_attestation,
)


def attest_sandbox_review(
    *,
    manifest_path: Path,
    private_key_path: Path,
    key_id: str,
    verifier: str,
    signed_at: datetime | None = None,
) -> dict[str, object]:
    resolved = manifest_path.resolve(strict=True)
    if resolved.name != "hardened_sandbox_review.manifest.json":
        raise ValueError("unexpected hardened sandbox manifest filename")
    manifest_bytes = resolved.read_bytes()
    manifest = StageThreeGateManifest.model_validate_json(manifest_bytes)
    evidence_path = (resolved.parent / manifest.evidence_path).resolve(strict=True)
    if not evidence_path.is_relative_to(resolved.parent):
        raise ValueError("hardened sandbox evidence path escapes its bundle")
    evidence_hash = f"sha256:{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}"
    if evidence_hash != manifest.evidence_hash:
        raise ValueError("hardened sandbox evidence hash mismatch")
    private_key = load_pem_private_key(
        private_key_path.resolve(strict=True).read_bytes(), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("sandbox review signing key must be Ed25519")
    statement = SandboxReviewAttestationStatement(
        verifier=verifier,
        key_id=key_id,
        manifest_hash=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        signed_at=signed_at or datetime.now(UTC),
    )
    signature = private_key.sign(serialize_sandbox_attestation(statement))
    attestation = SandboxReviewAttestation(
        **statement.model_dump(),
        signature=f"base64:{base64.b64encode(signature).decode()}",
    )
    output_path = resolved.with_name(f"{manifest.gate_id}.{key_id}.attestation.json")
    if output_path.exists():
        raise FileExistsError("sandbox review attestation already exists")
    output_path.write_text(
        json.dumps(attestation.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return {
        "gate_id": manifest.gate_id,
        "verifier": verifier,
        "key_id": key_id,
        "attestation_path": str(output_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--verifier", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            attest_sandbox_review(
                manifest_path=args.manifest,
                private_key_path=args.private_key,
                key_id=args.key_id,
                verifier=args.verifier,
            ),
            indent=2,
            sort_keys=True,
        )
    )
