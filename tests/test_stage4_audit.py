import json
from uuid import uuid4

from generaldataagent.contracts import AuditEvent
from generaldataagent.observability.audit import MinimalAuditSink


def test_minimal_audit_drops_free_text_nested_secrets_and_wrong_types(tmp_path):
    path = tmp_path / "audit.jsonl"
    event = AuditEvent(
        event_type="tool.completed",
        task_id=uuid4(),
        actor_id="opaque-user",
        tenant_id="opaque-tenant",
        trace_id="opaque-trace",
        payload={
            "sql": "SELECT secret FROM users",
            "password": "sensitive",
            "nested": {"token": "sensitive"},
            "row_count": "sensitive",
            "duration_ms": 12,
            "success": True,
            "allowed": "sensitive",
            "cost_microunits": -1,
            "size_bytes": True,
        },
    )
    sink = MinimalAuditSink(path)
    sink.emit(event)
    sink.emit(event)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["payload"] == {"duration_ms": 12, "success": True}
    assert "sensitive" not in path.read_text()
    assert event.payload["password"] == "sensitive"
