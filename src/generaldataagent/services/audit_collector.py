"""Independent append-only audit collector with signed durable acknowledgements."""

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import SecretStr

from generaldataagent.contracts import AuditEvent
from generaldataagent.observability.verified_audit import AuditReceipt, receipt_statement


class AuditCollector:
    """Run under an identity independent of the analytics application.

    The application receives only an append token and public receipt verification keys.
    Collector database, private key, backups and retention belong to audit operators.
    """

    def __init__(self, path: Path, *, signer_id: str, signing_key: Ed25519PrivateKey) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.signer_id, self.signing_key = path, signer_id, signing_key
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit_entries ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, "
                "event_hash TEXT NOT NULL, canonical BLOB NOT NULL, "
                "previous_hash TEXT NOT NULL, chain_hash TEXT NOT NULL, receipt_json TEXT NOT NULL)"
            )

    def append(self, event: AuditEvent) -> AuditReceipt:
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (event.actor_id, event.tenant_id, event.trace_id)
        ) or event.event_type not in {"policy.decision", "approval.consumed", "security.event"}:
            raise ValueError("audit_envelope_must_be_minimized")
        for key, value in event.payload.items():
            valid = (
                key in {"row_count", "duration_ms", "size_bytes"}
                and type(value) is int
                and value >= 0
            )
            valid = valid or (
                key in {"decision", "risk_level"}
                and value
                in ("allow", "deny", "require_approval", "approved", "r0", "r1", "r2", "r3", "r4")
            )
            valid = valid or (
                key == "request_fingerprint"
                and isinstance(value, str)
                and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))
            )
            if not valid:
                raise ValueError("audit_payload_must_be_minimized")
        content = json.dumps(
            event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(content).hexdigest()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT event_hash, receipt_json FROM audit_entries WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise PermissionError("audit_event_id_conflict")
                return AuditReceipt.model_validate_json(existing[1])
            last = db.execute(
                "SELECT sequence, chain_hash FROM audit_entries ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence, previous = (last[0] + 1, last[1]) if last else (1, "0" * 64)
            chain_hash = hashlib.sha256(f"{sequence}:{previous}:{digest}".encode()).hexdigest()
            receipt = AuditReceipt(
                event_id=event.event_id,
                event_sha256=digest,
                sequence=sequence,
                recorded_at=datetime.now(UTC),
                signer_id=self.signer_id,
                signature="",
            )
            receipt = receipt.model_copy(
                update={
                    "signature": base64.b64encode(
                        self.signing_key.sign(receipt_statement(receipt))
                    ).decode()
                }
            )
            db.execute(
                "INSERT INTO audit_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    str(event.event_id),
                    digest,
                    content,
                    previous,
                    chain_hash,
                    receipt.model_dump_json(),
                ),
            )
        return receipt

    def verify_chain(
        self, *, expected_sequence: int | None = None, expected_hash: str | None = None
    ) -> bool:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT sequence, event_hash, canonical, previous_hash, chain_hash "
                "FROM audit_entries ORDER BY sequence"
            ).fetchall()
        previous = "0" * 64
        for index, (sequence, digest, content, parent, chain) in enumerate(rows, start=1):
            if (
                sequence != index
                or parent != previous
                or hashlib.sha256(content).hexdigest() != digest
                or hashlib.sha256(f"{sequence}:{parent}:{digest}".encode()).hexdigest() != chain
            ):
                return False
            previous = chain
        return (expected_sequence is None or len(rows) == expected_sequence) and (
            expected_hash is None or previous == expected_hash
        )


def create_audit_collector_app(collector: AuditCollector, *, append_token: SecretStr) -> FastAPI:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", append_token.get_secret_value()):
        raise ValueError("strong independent append credential required")
    app = FastAPI(
        title="GeneralDataAgent independent audit collector",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/append")
    async def append(request: Request, authorization: str = Header(default="")):
        expected = "Bearer " + append_token.get_secret_value()
        if len(authorization) > 1024 or not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid_append_identity")
        # Bound streamed input even when Content-Length is absent or false.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                raise HTTPException(status_code=413, detail="audit_event_too_large")
            chunks.append(chunk)
        try:
            event = AuditEvent.model_validate_json(b"".join(chunks))
            return collector.append(event).model_dump(mode="json")
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail="audit_event_id_conflict") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_audit_event") from exc

    return app
