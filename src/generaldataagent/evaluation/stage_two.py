"""Validated evidence bundles for real-environment Stage 2 gates."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

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


def load_external_gate_results(
    evidence_dir: Path | None,
    *,
    now: datetime | None = None,
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

    checked_at = now or datetime.now(UTC)
    results: list[dict[str, object]] = []
    for gate_id, requirement in EXTERNAL_GATE_REQUIREMENTS.items():
        manifest_path = root / f"{gate_id}.manifest.json"
        if not manifest_path.exists():
            results.append(_not_run(gate_id))
            continue
        manifest = ExternalGateEvidence.model_validate_json(manifest_path.read_text())
        if manifest.gate_id != gate_id:
            raise ValueError(
                f"external evidence manifest {manifest_path.name} declares {manifest.gate_id}"
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
            }
        )
    return tuple(results)


def write_external_gate_manifest(path: Path, evidence: ExternalGateEvidence) -> None:
    """Write a canonical manifest for an externally produced evidence file."""

    path.write_text(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def _not_run(gate_id: ExternalGateId) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "status": "not_run",
        "required_evidence": EXTERNAL_GATE_REQUIREMENTS[gate_id],
    }
