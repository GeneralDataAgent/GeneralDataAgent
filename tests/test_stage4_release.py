from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.security.release import REQUIRED_EXERCISES, ReleaseEvidence, release_blockers


def test_release_gate_fails_closed_on_each_missing_production_measurement():
    now = datetime.now(UTC)
    evidence = ReleaseEvidence("abc", now, 100, 88, 0, 0, 0, 100, 99, REQUIRED_EXERCISES)
    assert release_blockers(evidence, revision="abc", now=now) == ()
    mutations = [
        dict(revision="old"),
        dict(recorded_at=now - timedelta(days=2)),
        dict(recorded_at=now + timedelta(seconds=1)),
        dict(task_count=89),
        dict(passed_tasks=87),
        dict(passed_tasks=101),
        dict(unauthorized_successes=1),
        dict(sensitive_leaks=1),
        dict(approval_bypasses=1),
        dict(recovery_attempts=0),
        dict(recovery_successes=98),
        dict(exercises=frozenset()),
        dict(blocking_findings=1),
    ]
    for mutation in mutations:
        assert release_blockers(replace(evidence, **mutation), revision="abc", now=now)


def test_kill_switch_is_dynamic_and_fails_closed():
    enabled = True
    client = TestClient(create_app(service_enabled=lambda: enabled))
    assert client.get("/v1/capabilities").status_code == 200
    enabled = False
    assert client.get("/v1/capabilities").status_code == 503
    assert client.get("/health").status_code == 200

    def broken():
        raise RuntimeError("control plane unavailable")

    client = TestClient(create_app(service_enabled=broken))
    assert client.get("/v1/capabilities").json() == {"detail": "service_disabled"}
