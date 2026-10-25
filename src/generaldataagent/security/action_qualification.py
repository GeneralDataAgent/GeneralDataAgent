"""Per-action independent qualification, bound to source/configuration/environment.

Synthetic local outbox receipts cannot satisfy these external receiver measurements.
Trust comes from operator-provisioned independent security and operations keys.
"""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field

from generaldataagent.contracts.business import ActionKind
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.release_bundle import ReleaseSignature


class ActionQualification(ContractModel):
    kind: ActionKind
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    configuration_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment: Literal["staging", "production"]
    source_system: str = Field(min_length=1, max_length=256)
    destination: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    trials: int = Field(ge=20, strict=True)
    duplicate_side_effects: int = Field(ge=0, strict=True)
    content_mismatches: int = Field(ge=0, strict=True)
    compensation_attempts: int = Field(ge=1, strict=True)
    compensation_successes: int = Field(ge=0, strict=True)
    complete_audit_records: int = Field(ge=0, strict=True)
    security_regressions: int = Field(ge=0, strict=True)
    identity_and_policy_rechecked: bool = Field(strict=True)
    expiry_and_cancel_tested: bool = Field(strict=True)
    independent_kill_switch_tested: bool = Field(strict=True)
    rate_limit_tested: bool = Field(strict=True)
    crash_reconciliation_tested: bool = Field(strict=True)
    receiver_receipts_verified: bool = Field(strict=True)
    native_authorization_verified: bool = Field(strict=True)
    business_metric: str = Field(min_length=1, max_length=128)
    baseline_value: float = Field(allow_inf_nan=False)
    measured_value: float = Field(allow_inf_nan=False)
    improvement_direction: Literal["increase", "decrease"]
    required_improvement: float = Field(gt=0, allow_inf_nan=False)
    underlying_evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QualifiedActionBundle(ContractModel):
    evidence: ActionQualification
    signatures: tuple[ReleaseSignature, ...] = Field(min_length=2, max_length=10)


def action_qualification_statement(bundle: QualifiedActionBundle) -> bytes:
    return json.dumps(
        bundle.model_dump(mode="json", exclude={"signatures"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def verify_action_qualification(
    bundle: QualifiedActionBundle,
    *,
    kind: str,
    revision: str,
    configuration_hash: str,
    environment: str,
    trusted_keys: Mapping[tuple[str, str], Ed25519PublicKey],
    now: datetime | None = None,
) -> tuple[str, ...]:
    now = now or datetime.now(UTC)
    evidence = bundle.evidence
    blockers = []
    if (
        evidence.kind != kind
        or evidence.revision != revision
        or evidence.configuration_hash != configuration_hash
        or evidence.environment != environment
    ):
        blockers.append("action_scope_or_version_mismatch")
    if (
        now.tzinfo is None
        or not evidence.observed_at <= now < evidence.valid_until
        or not timedelta(0) < evidence.valid_until - evidence.observed_at <= timedelta(days=1)
    ):
        blockers.append("action_evidence_expired_or_future")
    if (
        evidence.duplicate_side_effects
        or evidence.content_mismatches
        or evidence.security_regressions
        or evidence.complete_audit_records != evidence.trials
        or evidence.compensation_successes != evidence.compensation_attempts
        or not all(
            (
                evidence.identity_and_policy_rechecked,
                evidence.expiry_and_cancel_tested,
                evidence.independent_kill_switch_tested,
                evidence.rate_limit_tested,
                evidence.crash_reconciliation_tested,
                evidence.receiver_receipts_verified,
                evidence.native_authorization_verified,
            )
        )
    ):
        blockers.append("action_safety_or_compensation_failed")
    improvement = evidence.measured_value - evidence.baseline_value
    if evidence.improvement_direction == "decrease":
        improvement = -improvement
    if improvement < evidence.required_improvement:
        blockers.append("action_business_value_below_target")
    signers, roles, keys = set(), set(), set()
    try:
        statement = action_qualification_statement(bundle)
        for signature in bundle.signatures:
            key = trusted_keys.get((signature.signer_id, signature.role))
            if (
                key is None
                or signature.signer_id in signers
                or signature.signer_id == evidence.owner
                or key.public_bytes_raw() in keys
            ):
                raise ValueError("independent_action_signatures_required")
            key.verify(base64.b64decode(signature.signature, validate=True), statement)
            signers.add(signature.signer_id)
            roles.add(signature.role)
            keys.add(key.public_bytes_raw())
        if roles != {"security", "operations"}:
            raise ValueError("independent_security_and_operations_required")
    except Exception:
        blockers.append("independent_action_signatures_invalid")
    return tuple(blockers)
