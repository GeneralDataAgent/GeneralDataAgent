import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.contracts import AuditEvent
from generaldataagent.services.audit_collector import AuditCollector


def safe_event():
    return AuditEvent(
        event_type="security.event",
        task_id=uuid4(),
        actor_id="a" * 64,
        tenant_id="b" * 64,
        trace_id="c" * 64,
        payload={"decision": "allow"},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("signature", "AA=="),
        ("signer_id", "unknown"),
        ("sequence", 2),
        ("event_sha256", "f" * 64),
        ("recorded_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_stored_receipt_corruption_denies_chain_and_idempotent_ack(tmp_path, field, value):
    collector = AuditCollector(
        tmp_path / "audit.db", signer_id="one", signing_key=Ed25519PrivateKey.generate()
    )
    event = safe_event()
    receipt = collector.append(event).model_dump(mode="json")
    receipt[field] = value
    with sqlite3.connect(collector.path) as db:
        db.execute("UPDATE audit_entries SET receipt_json = ?", (json.dumps(receipt),))
    assert not collector.verify_chain()
    with pytest.raises(PermissionError, match="^audit_stored_receipt_invalid$"):
        collector.append(event)


def test_rehashing_altered_event_cannot_replace_signed_evidence(tmp_path):
    collector = AuditCollector(
        tmp_path / "audit.db", signer_id="one", signing_key=Ed25519PrivateKey.generate()
    )
    event = safe_event()
    collector.append(event)
    altered = event.model_copy(update={"payload": {"decision": "deny"}})
    content = json.dumps(
        altered.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    chain = hashlib.sha256(f"1:{'0' * 64}:{digest}".encode()).hexdigest()
    with sqlite3.connect(collector.path) as db:
        db.execute(
            "UPDATE audit_entries SET canonical = ?, event_hash = ?, chain_hash = ?",
            (content, digest, chain),
        )
    assert not collector.verify_chain()


def test_rotated_collector_requires_explicit_historical_public_key(tmp_path):
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    path = tmp_path / "audit.db"
    first = AuditCollector(path, signer_id="old", signing_key=old)
    event = safe_event()
    original = first.append(event)
    rotated = AuditCollector(
        path, signer_id="new", signing_key=new, trusted_receipt_keys={"old": old.public_key()}
    )
    rotated.append(safe_event())
    assert rotated.verify_chain(expected_sequence=2)
    assert rotated.append(event) == original
    untrusted = AuditCollector(path, signer_id="new", signing_key=new)
    assert not untrusted.verify_chain()
    with pytest.raises(PermissionError):
        untrusted.append(event)
