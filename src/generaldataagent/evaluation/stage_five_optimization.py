"""Independent held-out cost/latency comparison; local estimates never qualify as billing."""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field

from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.release_bundle import ReleaseSignature


class OptimizationEvidence(ContractModel):
    proof_type: Literal["stage5_optimization"] = "stage5_optimization"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    configuration_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    dataset_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment: Literal["staging", "production"]
    source_system: str = Field(min_length=1, max_length=256)
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    task_count: int = Field(ge=90, strict=True)
    baseline_passed_tasks: int = Field(ge=0, strict=True)
    optimized_passed_tasks: int = Field(ge=0, strict=True)
    minimum_success_rate: float = Field(ge=0.88, le=1, allow_inf_nan=False)
    baseline_cost_microunits: int = Field(ge=1, strict=True)
    optimized_cost_microunits: int = Field(ge=0, strict=True)
    baseline_p95_ms: float = Field(gt=0, allow_inf_nan=False)
    optimized_p95_ms: float = Field(gt=0, allow_inf_nan=False)
    minimum_cost_reduction_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    minimum_latency_reduction_fraction: float = Field(ge=0, lt=1, allow_inf_nan=False)
    safety_regressions: int = Field(ge=0, strict=True)
    measured_billing_verified: bool = Field(strict=True)
    cache_authorization_verified: bool = Field(strict=True)
    task_budget_verified: bool = Field(strict=True)
    underlying_evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class OptimizationBundle(ContractModel):
    evidence: OptimizationEvidence
    signatures: tuple[ReleaseSignature, ...] = Field(min_length=2, max_length=10)


def optimization_statement(bundle: OptimizationBundle) -> bytes:
    return json.dumps(
        bundle.model_dump(mode="json", exclude={"signatures"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def verify_optimization(
    bundle: OptimizationBundle,
    *,
    revision: str,
    configuration_hash: str,
    dataset_hash: str,
    environment: str,
    trusted_keys: Mapping[tuple[str, str], Ed25519PublicKey],
    now: datetime | None = None,
) -> tuple[str, ...]:
    now = now or datetime.now(UTC)
    e = bundle.evidence
    blockers = []
    if (
        e.revision != revision
        or e.configuration_hash != configuration_hash
        or e.dataset_hash != dataset_hash
        or e.environment != environment
    ):
        blockers.append("optimization_scope_or_version_mismatch")
    if (
        now.tzinfo is None
        or not e.observed_at <= now < e.valid_until
        or not timedelta(0) < e.valid_until - e.observed_at <= timedelta(days=1)
    ):
        blockers.append("optimization_evidence_expired_or_future")
    if (
        not 0 <= e.baseline_passed_tasks <= e.task_count
        or not e.baseline_passed_tasks <= e.optimized_passed_tasks <= e.task_count
        or e.optimized_passed_tasks / e.task_count < e.minimum_success_rate
        or e.safety_regressions
        or not all(
            (e.measured_billing_verified, e.cache_authorization_verified, e.task_budget_verified)
        )
    ):
        blockers.append("optimization_quality_safety_or_measurement_failed")
    if (
        max(e.minimum_cost_reduction_fraction, e.minimum_latency_reduction_fraction) <= 0
        or e.optimized_cost_microunits
        > e.baseline_cost_microunits * (1 - e.minimum_cost_reduction_fraction)
        or e.optimized_p95_ms > e.baseline_p95_ms * (1 - e.minimum_latency_reduction_fraction)
    ):
        blockers.append("optimization_target_not_met")
    signers, roles, keys = set(), set(), set()
    try:
        statement = optimization_statement(bundle)
        for signature in bundle.signatures:
            key = trusted_keys.get((signature.signer_id, signature.role))
            if key is None or signature.signer_id in signers or key.public_bytes_raw() in keys:
                raise ValueError("independent_optimization_signatures_required")
            key.verify(base64.b64decode(signature.signature, validate=True), statement)
            signers.add(signature.signer_id)
            roles.add(signature.role)
            keys.add(key.public_bytes_raw())
        if roles != {"security", "operations"}:
            raise ValueError("security_and_operations_required")
    except Exception:
        blockers.append("independent_optimization_signatures_invalid")
    return tuple(blockers)
