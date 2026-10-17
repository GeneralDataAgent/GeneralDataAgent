"""Live file-backed production admission; revoked trust or expired proof stops new work."""

import base64
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from generaldataagent.security.release_bundle import ReleaseBundle, verify_release_bundle


class TrustedReleaseKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    signer_id: str = Field(min_length=1, max_length=128)
    role: Literal["security", "operations"]
    public_key: str = Field(max_length=128)


def load_release_keys(path: Path) -> dict[tuple[str, str], Ed25519PublicKey]:
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("release trust registry too large")
    records = TypeAdapter(list[TrustedReleaseKey]).validate_json(raw)
    if not 2 <= len(records) <= 100:
        raise ValueError("bounded independent signer registry required")
    keys = {}
    for record in records:
        identity = (record.signer_id, record.role)
        if identity in keys:
            raise ValueError("duplicate signer identity")
        keys[identity] = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(record.public_key, validate=True)
        )
    return keys


class FileReleaseAdmission:
    """Paths are operator-owned and outside agent mounts; no cached permissive fallback."""

    def __init__(
        self,
        *,
        bundle_path: Path,
        trust_path: Path,
        revision: str,
        environment: Literal["staging", "production"],
    ):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("exact committed source revision required")
        self.bundle_path, self.trust_path = bundle_path, trust_path
        self.revision, self.environment = revision, environment

    def allowed(self, *, now: datetime | None = None) -> bool:
        try:
            raw = self.bundle_path.read_bytes()
            if len(raw) > 1024 * 1024:
                return False
            bundle = ReleaseBundle.model_validate_json(raw)
            keys = load_release_keys(self.trust_path)
            return not verify_release_bundle(
                bundle,
                revision=self.revision,
                environment=self.environment,
                trusted_keys=keys,
                now=now or datetime.now(UTC),
            )
        except Exception:
            return False
