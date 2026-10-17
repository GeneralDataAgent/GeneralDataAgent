"""Independent audit transport with event-bound, signed append receipts."""

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from generaldataagent.contracts import AuditEvent


class AuditUnavailableError(PermissionError):
    pass


class AuditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(gt=0)
    recorded_at: AwareDatetime
    signer_id: str = Field(min_length=1, max_length=128)
    signature: str


def receipt_statement(receipt: AuditReceipt) -> bytes:
    return json.dumps(
        receipt.model_dump(mode="json", exclude={"signature"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class VerifiedAuditSink:
    """Production port: app can append, but cannot forge independent append receipts.

    transport must use an append-only service identity; read/delete/admin credentials
    never belong to the application. The signing private key belongs to that service.
    Receipt verification proves acknowledgement, not the remote service's storage policy.
    """

    def __init__(
        self,
        *,
        transport: Callable[[bytes], AuditReceipt],
        trusted_keys: Mapping[str, Ed25519PublicKey],
        pseudonym_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(pseudonym_key) < 32 or not trusted_keys:
            raise ValueError("independent audit trust and a strong pseudonym key are required")
        self.transport, self.trusted_keys = transport, dict(trusted_keys)
        self.pseudonym_key = pseudonym_key
        self.clock = clock or (lambda: datetime.now(UTC))

    def emit(self, event: AuditEvent) -> None:
        # No free-form payload or envelope identifiers leave the application.
        payload = {}
        for key in ("decision", "risk_level"):
            value = event.payload.get(key)
            if value in (
                "allow",
                "deny",
                "require_approval",
                "approved",
                "r0",
                "r1",
                "r2",
                "r3",
                "r4",
            ):
                payload[key] = value
        for key in ("row_count", "duration_ms", "size_bytes"):
            value = event.payload.get(key)
            if type(value) is int and value >= 0:
                payload[key] = value
        safe = event.model_dump(mode="json")
        for key in ("actor_id", "tenant_id", "trace_id"):
            safe[key] = hmac.new(self.pseudonym_key, safe[key].encode(), hashlib.sha256).hexdigest()
        safe["event_type"] = (
            event.event_type
            if event.event_type in {"policy.decision", "approval.consumed"}
            else "security.event"
        )
        safe["payload"] = payload
        content = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
        try:
            receipt = AuditReceipt.model_validate(self.transport(content))
            key = self.trusted_keys.get(receipt.signer_id)
            if key is None or receipt.event_id != event.event_id:
                raise ValueError("untrusted receipt")
            if receipt.event_sha256 != hashlib.sha256(content).hexdigest():
                raise ValueError("receipt content mismatch")
            now = self.clock()
            if now.tzinfo is None or not timedelta(0) <= now - receipt.recorded_at <= timedelta(
                minutes=5
            ):
                raise ValueError("receipt time invalid")
            key.verify(
                base64.b64decode(receipt.signature, validate=True), receipt_statement(receipt)
            )
        except Exception:
            # Do not log service errors that may echo raw content or authentication headers.
            raise AuditUnavailableError("independent_audit_unavailable") from None
