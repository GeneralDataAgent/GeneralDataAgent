import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from generaldataagent.contracts import AuditEvent
from generaldataagent.observability.verified_audit import AuditReceipt, VerifiedAuditSink
from generaldataagent.services.audit_collector import AuditCollector, create_audit_collector_app


def test_independent_collector_signed_ack_idempotency_minimization_and_chain(tmp_path):
    private = Ed25519PrivateKey.generate()
    collector = AuditCollector(
        tmp_path / "independent.db", signer_id="independent", signing_key=private
    )
    token = "append-only-fixture-token-1234567890"
    client = TestClient(create_audit_collector_app(collector, append_token=SecretStr(token)))
    captured = []

    def transport(content):
        captured.append(content)
        response = client.post(
            "/v1/append", content=content, headers={"authorization": "Bearer " + token}
        )
        assert response.status_code == 200, response.json()
        return AuditReceipt.model_validate(response.json())

    sink = VerifiedAuditSink(
        transport=transport,
        trusted_keys={"independent": private.public_key()},
        pseudonym_key=b"p" * 32,
    )
    event = AuditEvent(
        event_type="policy.decision",
        task_id=uuid4(),
        actor_id="private-user",
        tenant_id="private-tenant",
        trace_id="private-trace",
        payload={"decision": "allow", "password": "must-not-leave-app"},
    )
    sink.emit(event)
    safe_event = AuditEvent.model_validate_json(captured[0])

    def append(_):
        return AuditCollector(collector.path, signer_id="independent", signing_key=private).append(
            safe_event
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(append, range(16)))
    assert {receipt.sequence for receipt in receipts} == {1}
    assert collector.verify_chain(expected_sequence=1)
    assert "must-not-leave-app" not in collector.path.read_bytes().decode(errors="ignore")
    with pytest.raises(PermissionError):
        collector.append(safe_event.model_copy(update={"payload": {"decision": "deny"}}))
    with pytest.raises(ValueError):
        collector.append(event)
    assert client.post("/v1/append", json=safe_event.model_dump(mode="json")).status_code == 401
    headers = {"authorization": "Bearer " + token}
    assert client.post("/v1/append", content=b"x" * 65537, headers=headers).status_code == 413
    assert client.post("/v1/append", content=b"not-json", headers=headers).status_code == 422
    assert client.delete("/v1/append", headers=headers).status_code == 405
    assert client.get("/v1/entries", headers=headers).status_code == 404
    assert client.get("/health").status_code == 200
    with sqlite3.connect(collector.path) as db:
        db.execute("UPDATE audit_entries SET event_hash = ?", ("0" * 64,))
    assert not collector.verify_chain()
