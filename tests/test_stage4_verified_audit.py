import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.contracts import AuditEvent, ToolRequest
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    AuditUnavailableError,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.policy import PolicyEngine
from generaldataagent.tools.gateway import ToolGateway


def test_independent_receipts_minimize_payload_and_block_tool_on_failure():
    now = datetime.now(UTC)
    private = Ed25519PrivateKey.generate()
    captured = []
    mode = "valid"

    def transport(content):
        captured.append(content)
        value = json.loads(content)
        receipt = AuditReceipt(
            event_id=value["event_id"],
            event_sha256=hashlib.sha256(content).hexdigest(),
            sequence=1,
            recorded_at=now,
            signer_id="audit",
            signature="",
        )
        changes = {
            "wrong_event": {"event_id": uuid4()},
            "wrong_hash": {"event_sha256": "0" * 64},
            "stale": {"recorded_at": now - timedelta(minutes=6)},
            "future": {"recorded_at": now + timedelta(seconds=1)},
            "unknown_signer": {"signer_id": "attacker"},
        }
        if mode == "outage":
            raise RuntimeError("secret credential in remote error")
        receipt = receipt.model_copy(update=changes.get(mode, {}))
        signature = base64.b64encode(private.sign(receipt_statement(receipt))).decode()
        return receipt.model_copy(update={"signature": signature if mode != "forged" else "AAAA"})

    sink = VerifiedAuditSink(
        transport=transport,
        trusted_keys={"audit": private.public_key()},
        pseudonym_key=b"a" * 32,
        clock=lambda: now,
    )
    event = AuditEvent(
        event_type="arbitrary password",
        task_id=uuid4(),
        actor_id="alice@example.com",
        tenant_id="sensitive-tenant",
        trace_id="private-trace",
        payload={
            "sql": "SELECT private",
            "password": "sensitive",
            "decision": "allow",
            "row_count": 2,
        },
    )
    sink.emit(event)
    value = json.loads(captured[0])
    assert value["payload"] == {"decision": "allow", "row_count": 2}
    assert value["event_type"] == "security.event"
    assert b"alice@example.com" not in captured[0]
    gateway = ToolGateway(PolicyEngine(), audit=sink, production_mode=True)
    calls = []
    gateway.register("sql.query", lambda arguments: calls.append(arguments))
    request = ToolRequest(
        task_id=uuid4(),
        user_id="u",
        tenant_id="t",
        project_id="p",
        agent_role="data_analyst",
        tool_name="sql.query",
        action="query",
        resource="orders",
        data_domains=("d",),
    )
    for failure_mode in (
        "wrong_event",
        "wrong_hash",
        "stale",
        "future",
        "unknown_signer",
        "outage",
        "forged",
    ):
        mode = failure_mode
        with pytest.raises(AuditUnavailableError, match="independent_audit_unavailable"):
            gateway.invoke(request, trace_id="trace")
    assert calls == []
    mode = "valid"
    gateway.invoke(request, trace_id="trace")
    assert len(calls) == 1
    with pytest.raises(ValueError):
        ToolGateway(PolicyEngine(), production_mode=True)
    with pytest.raises(ValueError):
        VerifiedAuditSink(transport=transport, trusted_keys={}, pseudonym_key=b"short")
