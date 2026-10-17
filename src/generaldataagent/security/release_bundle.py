"""Two-party signed release evidence bound to an exact repository revision."""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from generaldataagent.security.release import ReleaseEvidence, release_blockers


class ReleaseSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    signer_id: str = Field(min_length=1, max_length=128)
    role: Literal["security", "operations"]
    signature: str


class ReleaseBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    environment: Literal["staging", "production"]
    evidence: ReleaseEvidence
    valid_until: AwareDatetime
    signatures: tuple[ReleaseSignature, ...] = Field(min_length=2, max_length=10)


def release_statement(bundle: ReleaseBundle) -> bytes:
    value = bundle.model_dump(mode="json", exclude={"signatures"})
    value["evidence"]["exercises"] = sorted(value["evidence"]["exercises"])
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def verify_release_bundle(
    bundle: ReleaseBundle,
    *,
    revision: str,
    environment: str,
    trusted_keys: Mapping[tuple[str, str], Ed25519PublicKey],
    now: datetime | None = None,
) -> tuple[str, ...]:
    now = now or datetime.now(UTC)
    reasons = list(release_blockers(bundle.evidence, revision=revision, now=now))
    if bundle.environment != environment:
        reasons.append("environment_mismatch")
    if (
        now.tzinfo is None
        or bundle.valid_until <= now
        or bundle.evidence.recorded_at.tzinfo is None
        or not timedelta(0) < bundle.valid_until - bundle.evidence.recorded_at <= timedelta(days=1)
    ):
        reasons.append("invalid_release_window")
    signers, roles, keys = set(), set(), set()
    try:
        statement = release_statement(bundle)
        for signature in bundle.signatures:
            key = trusted_keys.get((signature.signer_id, signature.role))
            if key is None or signature.signer_id in signers:
                raise ValueError("untrusted or repeated signer")
            raw_key = key.public_bytes_raw()
            if raw_key in keys:
                raise ValueError("signing keys must be independent")
            key.verify(base64.b64decode(signature.signature, validate=True), statement)
            signers.add(signature.signer_id)
            roles.add(signature.role)
            keys.add(raw_key)
        if roles != {"security", "operations"}:
            raise ValueError("both independent roles are required")
    except Exception:
        reasons.append("independent_release_signatures_invalid")
    return tuple(reasons)
