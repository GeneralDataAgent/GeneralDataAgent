from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.observability.alerts import (
    AlertDeliveryError,
    AlertReceipt,
    CriticalAlert,
    CriticalAlertRouter,
)


def test_alert_retry_restart_idempotency_and_five_minute_delivery_measurement(tmp_path):
    now = datetime.now(UTC)
    destination = {}
    outage = True
    calls = []

    def sender(alert):
        calls.append(alert.event_id)
        if outage:
            raise RuntimeError("remote secret must never be echoed")
        # A real destination must implement this idempotency contract.
        return destination.setdefault(
            alert.event_id, AlertReceipt(event_id=alert.event_id, delivered_at=now)
        )

    path = tmp_path / "alerts"
    router = CriticalAlertRouter(path, send=sender, clock=lambda: now)
    alert = CriticalAlert(kind="audit_outage", occurred_at=now - timedelta(seconds=60))
    with pytest.raises(AlertDeliveryError, match="critical_alert_delivery_failed"):
        router.deliver(alert)
    assert router.measurements()["pending"] == 1
    outage = False
    router = CriticalAlertRouter(path, send=sender, clock=lambda: now)
    receipt = router.deliver(alert)
    assert router.deliver(alert) == receipt
    assert len(calls) == 2 and len(destination) == 1
    assert router.measurements() == {
        "total": 1,
        "pending": 0,
        "max_delivery_seconds": 60.0,
        "late_deliveries": 0,
    }
    late = CriticalAlert(kind="slo_breach", occurred_at=now - timedelta(seconds=301))
    router.deliver(late)
    assert router.measurements()["late_deliveries"] == 1
    with pytest.raises(AlertDeliveryError, match="conflict"):
        router.deliver(alert.model_copy(update={"kind": "credential_leak"}))
    with pytest.raises(AlertDeliveryError, match="time"):
        router.deliver(CriticalAlert(kind="cost_budget", occurred_at=now + timedelta(seconds=1)))
    invalid = CriticalAlertRouter(
        tmp_path / "bad",
        send=lambda alert: AlertReceipt(event_id=uuid4(), delivered_at=now),
        clock=lambda: now,
    )
    with pytest.raises(AlertDeliveryError):
        invalid.deliver(alert)


def test_audit_outage_emits_one_critical_signal_without_executing_tools(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from generaldataagent.contracts import ToolRequest
    from generaldataagent.observability.verified_audit import (
        AuditUnavailableError,
        VerifiedAuditSink,
    )
    from generaldataagent.policy import PolicyEngine
    from generaldataagent.tools.gateway import ToolGateway

    now = datetime.now(UTC)
    notifications = []

    def sender(event):
        notifications.append(event)
        return AlertReceipt(event_id=event.event_id, delivered_at=now)

    router = CriticalAlertRouter(tmp_path / "alerts", send=sender, clock=lambda: now)

    def unavailable(_):
        raise RuntimeError("audit unavailable")

    sink = VerifiedAuditSink(
        transport=unavailable,
        trusted_keys={"audit": Ed25519PrivateKey.generate().public_key()},
        pseudonym_key=b"x" * 32,
    )
    gateway = ToolGateway(PolicyEngine(), audit=sink, alerts=router, production_mode=True)
    calls = []
    gateway.register("sql.query", lambda args: calls.append(args))
    request = ToolRequest(
        task_id=uuid4(),
        user_id="u",
        tenant_id="t",
        project_id="p",
        tool_name="sql.query",
        action="query",
        resource="orders",
        agent_role="data_analyst",
        data_domains=("d",),
    )
    for _ in range(3):
        with pytest.raises(AuditUnavailableError):
            gateway.invoke(request, trace_id="trace")
    assert not calls and len(notifications) == 1
    assert router.measurements()["max_delivery_seconds"] == 0.0
