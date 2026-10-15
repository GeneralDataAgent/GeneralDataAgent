"""Validated evidence bundles for real-environment Stage 2 gates."""

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.data_engineering import LineageAsset
from generaldataagent.contracts.models import ContractModel

ExternalGateId = Literal[
    "authoritative_profiler",
    "real_dbt_project",
    "authoritative_catalog",
    "external_approval_service",
    "production_scale_load",
]

EXTERNAL_GATE_REQUIREMENTS: dict[ExternalGateId, str] = {
    "authoritative_profiler": (
        "Compare profiles with an independent authoritative profiler at >=99% agreement."
    ),
    "real_dbt_project": (
        "Qualify dbt compile and selected tests on a real non-production project."
    ),
    "authoritative_catalog": (
        "Verify critical lineage dependencies against an authoritative catalog and Dashboards."
    ),
    "external_approval_service": (
        "Replace the local SQLite approval store with an independently authorized service."
    ),
    "production_scale_load": (
        "Run production-scale capacity, saturation, and recovery tests in the target environment."
    ),
}


class ExternalGateEvidence(ContractModel):
    gate_id: ExternalGateId
    status: Literal["passed", "failed"]
    source_system: str = Field(min_length=1, max_length=256)
    environment: Literal["non_production", "staging", "production"]
    verifier: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_path: str = Field(min_length=1, max_length=512)
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_at: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def validate_evidence_window_and_path(self) -> "ExternalGateEvidence":
        if self.valid_until <= self.observed_at:
            raise ValueError("external evidence must expire after it was observed")
        path = Path(self.evidence_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("external evidence path must stay inside its bundle directory")
        return self


class CatalogLineageExport(ContractModel):
    source_system: str = Field(min_length=1, max_length=256)
    assets: tuple[LineageAsset, ...] = Field(min_length=1)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_complete_graph(self) -> "CatalogLineageExport":
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("authoritative catalog asset ids must be unique")
        unknown = {
            dependency
            for asset in self.assets
            for dependency in asset.depends_on
            if dependency not in set(asset_ids)
        }
        if unknown:
            raise ValueError(f"authoritative catalog has unknown dependencies: {sorted(unknown)}")
        if not any(asset.asset_type == "dashboard" for asset in self.assets):
            raise ValueError("authoritative catalog must include at least one dashboard")
        return self


class ExternalApprovalTranscript(ContractModel):
    source_system: str = Field(min_length=1, max_length=256)
    environment: Literal["non_production", "staging", "production"]
    service_request_id: str = Field(min_length=1, max_length=256)
    approval_id: UUID
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=128)
    requester_id: str = Field(min_length=1, max_length=128)
    approver_id: str = Field(min_length=1, max_length=128)
    requested_at: AwareDatetime
    approved_at: AwareDatetime
    consumed_at: AwareDatetime
    expires_at: AwareDatetime
    request_bound: bool
    tenant_bound: bool
    self_approval_rejected: bool
    changed_request_rejected: bool
    replay_blocked: bool

    @model_validator(mode="after")
    def validate_event_order(self) -> "ExternalApprovalTranscript":
        if not self.requested_at <= self.approved_at <= self.consumed_at:
            raise ValueError("external approval event timestamps are out of order")
        if self.consumed_at > self.expires_at:
            raise ValueError("external approval was consumed after expiry")
        return self


class ProductionLoadTranscript(ContractModel):
    source_system: str = Field(min_length=1, max_length=256)
    environment: Literal["staging", "production"]
    run_id: str = Field(min_length=1, max_length=256)
    started_at: AwareDatetime
    completed_at: AwareDatetime
    request_count: int = Field(ge=1)
    max_workers: int = Field(ge=1)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    target_requests_per_second: float = Field(gt=0)
    observed_requests_per_second: float = Field(gt=0)
    p50_request_ms: float = Field(ge=0)
    p95_request_ms: float = Field(ge=0)
    p99_request_ms: float = Field(ge=0)
    max_p95_ms: float = Field(gt=0)
    saturation_signal: Literal["cpu", "memory", "connection_pool", "queue_depth"]
    saturation_observed: bool
    backpressure_activated: bool
    recovery_probe_succeeded: bool
    recovery_seconds: float = Field(ge=0)
    max_recovery_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_run_accounting(self) -> "ProductionLoadTranscript":
        if self.completed_at <= self.started_at:
            raise ValueError("production load run must complete after it starts")
        if self.succeeded + self.failed + self.cancelled != self.request_count:
            raise ValueError("production load outcomes must equal request_count")
        if self.artifact_count > self.succeeded:
            raise ValueError("production load artifacts cannot exceed successful requests")
        if not self.p50_request_ms <= self.p95_request_ms <= self.p99_request_ms:
            raise ValueError("production load latency percentiles must be ordered")
        return self


class TrustedExternalVerifier(ContractModel):
    verifier: str = Field(min_length=1, max_length=256)
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: str = Field(pattern=r"^base64:[A-Za-z0-9+/]+={0,2}$")
    allowed_gates: tuple[ExternalGateId, ...] = Field(min_length=1)
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_public_key_and_gates(self) -> "TrustedExternalVerifier":
        try:
            key_bytes = base64.b64decode(self.public_key.removeprefix("base64:"), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("trusted verifier public key must be valid base64") from exc
        if len(key_bytes) != 32:
            raise ValueError("trusted verifier Ed25519 public key must contain 32 bytes")
        if len(self.allowed_gates) != len(set(self.allowed_gates)):
            raise ValueError("trusted verifier allowed gates must be unique")
        if self.valid_until <= self.valid_from:
            raise ValueError("trusted verifier key must expire after it becomes valid")
        if self.revoked_at is not None and self.revoked_at <= self.valid_from:
            raise ValueError("trusted verifier revocation must occur after key activation")
        return self


class TrustedExternalVerifierRegistry(ContractModel):
    verifiers: tuple[TrustedExternalVerifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_identities(self) -> "TrustedExternalVerifierRegistry":
        identities = [(item.verifier, item.key_id) for item in self.verifiers]
        if len(identities) != len(set(identities)):
            raise ValueError("trusted verifier and key id pairs must be unique")
        return self


class ExternalEvidenceAttestationStatement(ContractModel):
    gate_id: ExternalGateId
    verifier: str = Field(min_length=1, max_length=256)
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    algorithm: Literal["ed25519"] = "ed25519"
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signed_at: AwareDatetime


class ExternalEvidenceAttestation(ExternalEvidenceAttestationStatement):
    signature: str = Field(pattern=r"^base64:[A-Za-z0-9+/]+={0,2}$")


def serialize_attestation_statement(
    statement: ExternalEvidenceAttestationStatement,
) -> bytes:
    """Serialize the complete signed statement deterministically."""

    return json.dumps(
        statement.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def load_external_gate_results(
    evidence_dir: Path | None,
    *,
    now: datetime | None = None,
    trusted_verifiers_path: Path | None = None,
) -> tuple[dict[str, object], ...]:
    """Load exact gate manifests and verify their referenced evidence bytes."""

    if evidence_dir is None:
        return tuple(_not_run(gate_id) for gate_id in EXTERNAL_GATE_REQUIREMENTS)
    root = evidence_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("external evidence path must be a directory")
    expected_names = {f"{gate_id}.manifest.json" for gate_id in EXTERNAL_GATE_REQUIREMENTS}
    unexpected = sorted(
        path.name for path in root.glob("*.manifest.json") if path.name not in expected_names
    )
    if unexpected:
        raise ValueError(f"unknown external gate manifests: {unexpected}")
    registry = _load_trusted_verifiers(trusted_verifiers_path)
    expected_attestation_names = {
        f"{gate_id}.attestation.json" for gate_id in EXTERNAL_GATE_REQUIREMENTS
    }
    unexpected_attestations = sorted(
        path.name
        for path in root.glob("*.attestation.json")
        if path.name not in expected_attestation_names
    )
    if unexpected_attestations:
        raise ValueError(f"unknown external gate attestations: {unexpected_attestations}")

    checked_at = now or datetime.now(UTC)
    if checked_at.utcoffset() is None:
        raise ValueError("external evidence check time must include a timezone")
    results: list[dict[str, object]] = []
    for gate_id, requirement in EXTERNAL_GATE_REQUIREMENTS.items():
        manifest_path = root / f"{gate_id}.manifest.json"
        if not manifest_path.exists():
            results.append(_not_run(gate_id))
            continue
        manifest_bytes = manifest_path.read_bytes()
        manifest = ExternalGateEvidence.model_validate_json(manifest_bytes)
        if manifest.gate_id != gate_id:
            raise ValueError(
                f"external evidence manifest {manifest_path.name} declares {manifest.gate_id}"
            )
        attestation = None
        if registry is not None:
            attestation = _verify_attestation(
                root=root,
                gate_id=gate_id,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                registry=registry,
                checked_at=checked_at,
            )
        evidence_path = (root / manifest.evidence_path).resolve(strict=True)
        if not evidence_path.is_relative_to(root) or not evidence_path.is_file():
            raise ValueError("external evidence file must stay inside its bundle directory")
        observed_hash = f"sha256:{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}"
        if observed_hash != manifest.evidence_hash:
            raise ValueError(f"external evidence hash mismatch for {gate_id}")
        status = "expired" if checked_at > manifest.valid_until else manifest.status
        results.append(
            {
                "gate_id": gate_id,
                "status": status,
                "required_evidence": requirement,
                "evidence": manifest.model_dump(mode="json"),
                **(
                    {"attestation": attestation.model_dump(mode="json")}
                    if attestation is not None
                    else {}
                ),
            }
        )
    return tuple(results)


def write_external_gate_manifest(path: Path, evidence: ExternalGateEvidence) -> None:
    """Write a canonical manifest for an externally produced evidence file."""

    path.write_text(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def _load_trusted_verifiers(
    trusted_verifiers_path: Path | None,
) -> TrustedExternalVerifierRegistry | None:
    if trusted_verifiers_path is None:
        return None
    registry_path = trusted_verifiers_path.resolve(strict=True)
    if not registry_path.is_file():
        raise ValueError("trusted verifier registry must be a file")
    return TrustedExternalVerifierRegistry.model_validate_json(registry_path.read_bytes())


def _verify_attestation(
    *,
    root: Path,
    gate_id: ExternalGateId,
    manifest: ExternalGateEvidence,
    manifest_bytes: bytes,
    registry: TrustedExternalVerifierRegistry,
    checked_at: datetime,
) -> ExternalEvidenceAttestation:
    attestation_path = root / f"{gate_id}.attestation.json"
    if not attestation_path.is_file():
        raise ValueError(f"missing external evidence attestation for {gate_id}")
    attestation = ExternalEvidenceAttestation.model_validate_json(attestation_path.read_bytes())
    if attestation.gate_id != gate_id or attestation.verifier != manifest.verifier:
        raise ValueError(f"external evidence attestation identity mismatch for {gate_id}")
    manifest_hash = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    if attestation.manifest_hash != manifest_hash:
        raise ValueError(f"external evidence manifest hash mismatch for {gate_id}")
    trusted = next(
        (
            item
            for item in registry.verifiers
            if item.verifier == attestation.verifier and item.key_id == attestation.key_id
        ),
        None,
    )
    if trusted is None:
        raise ValueError(f"untrusted external evidence verifier for {gate_id}")
    if gate_id not in trusted.allowed_gates:
        raise ValueError(f"external evidence verifier is not authorized for {gate_id}")
    if not trusted.valid_from <= attestation.signed_at <= trusted.valid_until:
        raise ValueError(f"external evidence signature is outside key validity for {gate_id}")
    if not manifest.observed_at <= attestation.signed_at <= checked_at:
        raise ValueError(f"external evidence signature time is invalid for {gate_id}")
    if attestation.signed_at > manifest.valid_until:
        raise ValueError(f"external evidence was signed after expiry for {gate_id}")
    if trusted.revoked_at is not None and checked_at >= trusted.revoked_at:
        raise ValueError(f"external evidence signing key is revoked for {gate_id}")
    public_key_bytes = base64.b64decode(trusted.public_key.removeprefix("base64:"), validate=True)
    signature = base64.b64decode(attestation.signature.removeprefix("base64:"), validate=True)
    statement = ExternalEvidenceAttestationStatement.model_validate(
        attestation.model_dump(exclude={"signature"})
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            serialize_attestation_statement(statement),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError(f"invalid external evidence signature for {gate_id}") from exc
    return attestation


def _not_run(gate_id: ExternalGateId) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "status": "not_run",
        "required_evidence": EXTERNAL_GATE_REQUIREMENTS[gate_id],
    }
