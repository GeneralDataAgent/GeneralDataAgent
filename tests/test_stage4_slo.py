from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.observability.slo import ServiceSLO
from generaldataagent.security.release import (
    REQUIRED_EXERCISES,
    ReleaseEvidence,
    SLOEvidence,
    release_blockers,
)


def test_bounded_slo_percentile_and_api_measurements():
    slo = ServiceSLO(capacity=100)
    assert slo.snapshot()["p95_seconds"] is None
    for i in range(100):
        slo.record(duration_seconds=i / 100, available=i < 99)
    assert slo.snapshot() == {"sample_count": 100, "available_count": 99, "p95_seconds": 0.94}
    slo.record(duration_seconds=1, available=True)
    assert slo.snapshot()["sample_count"] == 100
    for invalid in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            slo.record(duration_seconds=invalid, available=True)
    with pytest.raises(ValueError):
        ServiceSLO(capacity=1)
    client = TestClient(create_app(slo=slo, service_enabled=lambda: False))
    assert client.get("/v1/capabilities").status_code == 503
    assert slo.snapshot()["available_count"] == 98
    assert client.get("/health").status_code == 200
    enabled = TestClient(create_app(slo=ServiceSLO()))
    assert enabled.get("/v1/operations/slo").json()["sample_count"] == 0
    assert TestClient(create_app()).get("/v1/operations/slo").status_code == 503


def test_release_slo_thresholds_missing_nan_latency_alert_and_pilot():
    now = datetime.now(UTC)
    slo = SLOEvidence(30 * 86400, 0, 100, 3.0, 300.0, 14)
    evidence = ReleaseEvidence("abc", now, 100, 88, 0, 0, 0, 100, 99, REQUIRED_EXERCISES, slo=slo)
    assert not release_blockers(evidence, revision="abc", now=now)
    assert release_blockers(replace(evidence, slo=None), revision="abc", now=now)
    for mutation in (
        {"observed_seconds": 0},
        {"unavailable_seconds": 20000},
        {"event_samples": 99},
        {"p95_event_seconds": 3.01},
        {"p95_event_seconds": float("nan")},
        {"critical_alert_delivery_seconds": 301},
        {"pilot_clean_days": 13},
    ):
        changed = replace(evidence, slo=replace(slo, **mutation))
        assert "slo_or_pilot_below_threshold" in release_blockers(changed, revision="abc", now=now)
