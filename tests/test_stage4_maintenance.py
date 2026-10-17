from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.observability.alerts import (
    AlertDeliveryError,
    AlertReceipt,
    CriticalAlert,
    CriticalAlertRouter,
)
from generaldataagent.observability.maintenance import ServiceMaintenance
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.security.identity import SessionBroker


def test_maintenance_retries_original_alerts_and_cleans_only_configured_expired_stores(tmp_path):
    now = datetime.now(UTC)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    store = GovernedArtifactStore(
        tmp_path / "content",
        index_path=tmp_path / "index",
        actor=actor,
        retention_seconds=1,
        clock=lambda: now,
    )
    ref = store.put_json(
        task_id=uuid4(), artifact_type="report", value={"count": 1}, created_by="reporter"
    )
    held = store.put_json(
        task_id=uuid4(), artifact_type="report", value={"count": 2}, created_by="reporter"
    )
    store.set_legal_hold(held, held=True)
    failed = True
    sent = []

    def send(alert):
        sent.append(alert.event_id)
        if failed:
            raise RuntimeError("private transport error")
        return AlertReceipt(event_id=alert.event_id, delivered_at=now)

    router = CriticalAlertRouter(tmp_path / "alerts", send=send, clock=lambda: now)
    alert = CriticalAlert(kind="audit_outage", occurred_at=now)
    with pytest.raises(AlertDeliveryError):
        router.deliver(alert)
    with open_protected_checkpointer(
        tmp_path / "checkpoints", encryption_key=b"k" * 32, clock=lambda: now
    ) as saver:
        thread = str(uuid4())
        saver.register_thread(thread, actor, retention_seconds=1)
        now += timedelta(seconds=2)
        worker = ServiceMaintenance(artifacts=(store,), checkpoints=(saver,), alerts=router)
        status = worker.tick()
        assert status["failures"] == 1
        assert status["artifacts_deleted"] == 1 and status["checkpoints_deleted"] == 1
        assert store._path(held).exists() and not store._path(ref).exists()
        failed = False
        assert worker.tick()["alerts_delivered"] == 1
        assert router.measurements()["pending"] == 0
        assert sent == [alert.event_id] * 3
        assert worker.tick()["alerts_delivered"] == 1  # No extra delivery of completed intent.
    with pytest.raises(ValueError):
        router.retry_pending(limit=0)
    assert router.retry_pending(should_stop=lambda: True)["attempted"] == 0


def test_api_lifespan_runs_retries_without_new_requests_and_stops_worker(tmp_path):
    now = datetime.now(UTC)
    delivered = Event()
    outage = True

    def send(alert):
        if outage:
            raise RuntimeError("outage")
        delivered.set()
        return AlertReceipt(event_id=alert.event_id, delivered_at=datetime.now(UTC))

    router = CriticalAlertRouter(tmp_path / "alerts", send=send)
    with pytest.raises(AlertDeliveryError):
        router.deliver(CriticalAlert(kind="audit_outage", occurred_at=now))
    outage = False
    worker = ServiceMaintenance(alerts=router, interval_seconds=1)
    broker = SessionBroker()
    token = broker.issue(
        user_id="u",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"d"}),
        permissions=frozenset({"operations:read"}),
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "u",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "d",
    }
    with TestClient(create_app(session_broker=broker, maintenance=worker)) as api:
        assert delivered.wait(3)
        assert api.get("/v1/operations/maintenance", headers=headers).json()["running"] is True
        with pytest.raises(ValueError):
            worker.start()
    assert worker.status()["running"] is False
    assert router.measurements()["pending"] == 0
    assert TestClient(create_app()).get("/v1/operations/maintenance").status_code == 503
    with pytest.raises(ValueError):
        ServiceMaintenance(interval_seconds=0)
