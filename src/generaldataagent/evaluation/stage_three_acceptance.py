"""Signed external evidence for the Stage 3 hardened-sandbox gate."""

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.models import ContractModel

STAGE_THREE_GATE_ID = "hardened_sandbox_review"


class HardenedSandboxReview(ContractModel):
    source_system: str = Field(min_length=1, max_length=256)
    environment: Literal["non_production", "staging"]
    runtime_name: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    non_root_user: bool
    read_only_root_filesystem: bool
    network_default_deny: bool
    syscall_filter: bool
    no_new_privileges: bool
    resource_limits_enforced: bool
    secretless_workload: bool
    arbitrary_code_rejected: bool
    escape_tests_total: int = Field(ge=20)
    escape_tests_passed: int = Field(ge=0)
    reviewer: str = Field(min_length=1, max_length=256)
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def review_passes_every_control(self) -> "HardenedSandboxReview":
        controls = (
            self.non_root_user,
            self.read_only_root_filesystem,
            self.network_default_deny,
            self.syscall_filter,
            self.no_new_privileges,
            self.resource_limits_enforced,
            self.secretless_workload,
            self.arbitrary_code_rejected,
        )
        if not all(controls):
            raise ValueError("hardened sandbox review requires every security control")
        if self.escape_tests_passed != self.escape_tests_total:
            raise ValueError("hardened sandbox review requires every escape test to pass")
        return self


class StageThreeGateManifest(ContractModel):
    gate_id: Literal["hardened_sandbox_review"] = STAGE_THREE_GATE_ID
    status: Literal["passed"] = "passed"
    source_system: str = Field(min_length=1, max_length=256)
    verifier: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_path: str = Field(min_length=1, max_length=512)
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_at: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def validate_path_and_window(self) -> "StageThreeGateManifest":
        if self.valid_until <= self.observed_at:
            raise ValueError("Stage 3 evidence must expire after observation")
        path = Path(self.evidence_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Stage 3 evidence path must stay inside its bundle")
        return self


class TrustedSandboxReviewer(ContractModel):
    verifier: str = Field(min_length=1, max_length=256)
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    public_key: str = Field(pattern=r"^base64:[A-Za-z0-9+/]+={0,2}$")
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_key(self) -> "TrustedSandboxReviewer":
        try:
            key = base64.b64decode(self.public_key.removeprefix("base64:"), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("sandbox reviewer public key must be valid base64") from exc
        if len(key) != 32:
            raise ValueError("sandbox reviewer Ed25519 public key must contain 32 bytes")
        if self.valid_until <= self.valid_from:
            raise ValueError("sandbox reviewer key validity window is invalid")
        if self.revoked_at is not None and self.revoked_at <= self.valid_from:
            raise ValueError("sandbox reviewer revocation must follow activation")
        return self


class TrustedSandboxReviewerRegistry(ContractModel):
    reviewers: tuple[TrustedSandboxReviewer, ...] = Field(min_length=2)
    minimum_reviewers: int = Field(default=2, ge=2, le=8)

    @model_validator(mode="after")
    def validate_threshold(self) -> "TrustedSandboxReviewerRegistry":
        identities = [(item.verifier, item.key_id) for item in self.reviewers]
        if len(identities) != len(set(identities)):
            raise ValueError("sandbox reviewer and key pairs must be unique")
        if len({item.verifier for item in self.reviewers}) < self.minimum_reviewers:
            raise ValueError("sandbox review threshold exceeds distinct trusted reviewers")
        return self


class SandboxReviewAttestationStatement(ContractModel):
    gate_id: Literal["hardened_sandbox_review"] = STAGE_THREE_GATE_ID
    verifier: str = Field(min_length=1, max_length=256)
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signed_at: AwareDatetime


class SandboxReviewAttestation(SandboxReviewAttestationStatement):
    signature: str = Field(pattern=r"^base64:[A-Za-z0-9+/]+={0,2}$")


def serialize_sandbox_attestation(statement: SandboxReviewAttestationStatement) -> bytes:
    return json.dumps(
        statement.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def load_hardened_sandbox_gate(
    evidence_dir: Path | None,
    *,
    trusted_reviewers_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    if evidence_dir is None:
        return {
            "gate_id": STAGE_THREE_GATE_ID,
            "status": "not_run",
            "required_evidence": (
                "A hardened sandbox review signed by at least two independent trusted reviewers."
            ),
        }
    if trusted_reviewers_path is None:
        raise ValueError("hardened sandbox evidence requires a trusted reviewer registry")
    root = evidence_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Stage 3 evidence path must be a directory")
    unexpected_manifests = sorted(
        path.name
        for path in root.glob("*.manifest.json")
        if path.name != f"{STAGE_THREE_GATE_ID}.manifest.json"
    )
    if unexpected_manifests:
        raise ValueError(f"unknown Stage 3 evidence manifests: {unexpected_manifests}")
    manifest_path = root / f"{STAGE_THREE_GATE_ID}.manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = StageThreeGateManifest.model_validate_json(manifest_bytes)
    checked_at = now or datetime.now(UTC)
    if checked_at.utcoffset() is None:
        raise ValueError("Stage 3 evidence check time must include a timezone")
    if manifest.observed_at > checked_at:
        raise ValueError("hardened sandbox evidence is future-dated")
    if manifest.valid_until <= checked_at:
        raise ValueError("hardened sandbox evidence has expired")
    evidence_path = (root / manifest.evidence_path).resolve(strict=True)
    if not evidence_path.is_relative_to(root) or not evidence_path.is_file():
        raise ValueError("hardened sandbox evidence must stay inside its bundle")
    evidence_bytes = evidence_path.read_bytes()
    if _hash(evidence_bytes) != manifest.evidence_hash:
        raise ValueError("hardened sandbox evidence hash mismatch")
    review = HardenedSandboxReview.model_validate_json(evidence_bytes)
    if review.source_system != manifest.source_system or review.reviewer != manifest.verifier:
        raise ValueError("hardened sandbox review identity does not match its manifest")
    if review.reviewed_at != manifest.observed_at:
        raise ValueError("hardened sandbox review time does not match its manifest")
    registry = TrustedSandboxReviewerRegistry.model_validate_json(
        trusted_reviewers_path.resolve(strict=True).read_bytes()
    )
    attestations = _verify_attestations(
        root=root,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        registry=registry,
        checked_at=checked_at,
    )
    return {
        "gate_id": STAGE_THREE_GATE_ID,
        "status": "passed",
        "manifest_hash": _hash(manifest_bytes),
        "evidence_hash": manifest.evidence_hash,
        "image_digest": review.image_digest,
        "reviewers": [item.verifier for item in attestations],
        "escape_tests": review.escape_tests_total,
        "valid_until": manifest.valid_until.isoformat(),
    }


def _verify_attestations(
    *,
    root: Path,
    manifest: StageThreeGateManifest,
    manifest_bytes: bytes,
    registry: TrustedSandboxReviewerRegistry,
    checked_at: datetime,
) -> tuple[SandboxReviewAttestation, ...]:
    paths = sorted(root.glob(f"{STAGE_THREE_GATE_ID}.*.attestation.json"))
    unexpected = sorted(
        path.name for path in root.glob("*.attestation.json") if path not in set(paths)
    )
    if unexpected:
        raise ValueError(f"unknown hardened sandbox attestations: {unexpected}")
    attestations = tuple(
        _verify_attestation(
            path=path,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            registry=registry,
            checked_at=checked_at,
        )
        for path in paths
    )
    identities = {item.verifier for item in attestations}
    if manifest.verifier not in identities:
        raise ValueError("manifest verifier must sign the hardened sandbox review")
    if len(identities) < registry.minimum_reviewers:
        raise ValueError(
            "hardened sandbox reviewer threshold not met: "
            f"{len(identities)}/{registry.minimum_reviewers}"
        )
    return attestations


def _verify_attestation(
    *,
    path: Path,
    manifest: StageThreeGateManifest,
    manifest_bytes: bytes,
    registry: TrustedSandboxReviewerRegistry,
    checked_at: datetime,
) -> SandboxReviewAttestation:
    attestation = SandboxReviewAttestation.model_validate_json(path.read_bytes())
    if path.name != f"{STAGE_THREE_GATE_ID}.{attestation.key_id}.attestation.json":
        raise ValueError("hardened sandbox attestation filename mismatch")
    if attestation.manifest_hash != _hash(manifest_bytes):
        raise ValueError("hardened sandbox manifest hash mismatch")
    trusted = next(
        (
            item
            for item in registry.reviewers
            if item.verifier == attestation.verifier and item.key_id == attestation.key_id
        ),
        None,
    )
    if trusted is None:
        raise ValueError("untrusted hardened sandbox reviewer")
    if not trusted.valid_from <= attestation.signed_at <= trusted.valid_until:
        raise ValueError("hardened sandbox signature is outside key validity")
    if trusted.revoked_at is not None and attestation.signed_at >= trusted.revoked_at:
        raise ValueError("hardened sandbox reviewer key is revoked")
    if not manifest.observed_at <= attestation.signed_at <= checked_at:
        raise ValueError("hardened sandbox signature time is invalid")
    if attestation.signed_at > manifest.valid_until:
        raise ValueError("hardened sandbox review was signed after expiry")
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(trusted.public_key.removeprefix("base64:"), validate=True)
    )
    signature = base64.b64decode(attestation.signature.removeprefix("base64:"), validate=True)
    statement = SandboxReviewAttestationStatement.model_validate(
        attestation.model_dump(exclude={"signature"})
    )
    try:
        public_key.verify(signature, serialize_sandbox_attestation(statement))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("invalid hardened sandbox review signature") from exc
    return attestation


def _hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
