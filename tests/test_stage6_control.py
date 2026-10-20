"""Stop repeated terminal billing/auth failures without hiding unfinished tasks."""

import pytest

from generaldataagent.evaluation.stage_six_control import (
    ServiceCircuitBreaker,
    not_attempted_result,
)


def failed(status):
    return {"failure": "model_error", "trace": [{"status": "error", "http_status": status}]}


@pytest.mark.parametrize("status", [401, 402, 403, 404])
def test_terminal_errors_open_immediately(status):
    circuit = ServiceCircuitBreaker()
    circuit.observe(failed(status))
    assert circuit.reason == f"provider_http_{status}"
    circuit.observe({"failure": None, "trace": []})
    assert circuit.reason == f"provider_http_{status}"
    result = not_attempted_result(circuit.reason)
    assert not result["attempted"]
    assert result["failure"] == "service_circuit_open"
    assert result["cost_usd"] == 0


def test_transient_failures_are_consecutive_and_not_quality_errors():
    c = ServiceCircuitBreaker(max_consecutive_failures=2)
    c.observe(failed(429))
    assert c.reason is None
    c.observe({"failure": "answer_mismatch", "trace": []})
    c.observe(failed(429))
    assert c.reason is None
    c.observe(failed(429))
    assert c.reason == "repeated_provider_failure"
    with pytest.raises(ValueError):
        ServiceCircuitBreaker(max_consecutive_failures=0)


def test_pacing_is_shared_between_instances_and_does_not_reserve_expired_slot(tmp_path):
    from generaldataagent.evaluation.stage_six_control import SharedRequestPacer

    path = tmp_path / "pacing.sqlite"
    a = SharedRequestPacer(path, scope="endpoint", interval_seconds=2)
    b = SharedRequestPacer(path, scope="endpoint", interval_seconds=2)
    assert a.reserve(now=100, max_wait_seconds=10) == 0
    assert b.reserve(now=100, max_wait_seconds=10) == 2
    with pytest.raises(TimeoutError):
        a.reserve(now=100, max_wait_seconds=1)
    assert b.reserve(now=100, max_wait_seconds=10) == 4
    other = SharedRequestPacer(path, scope="other_endpoint", interval_seconds=2)
    assert other.reserve(now=100, max_wait_seconds=10) == 0


def test_conservative_long_pacing_still_rejects_slots_outside_case_deadline(tmp_path):
    from generaldataagent.evaluation.stage_six_control import SharedRequestPacer

    pacer = SharedRequestPacer(tmp_path / "slow.sqlite", scope="model", interval_seconds=16)
    assert pacer.reserve(now=100, max_wait_seconds=30) == 0
    with pytest.raises(TimeoutError):
        pacer.reserve(now=100, max_wait_seconds=10)
    assert pacer.reserve(now=100, max_wait_seconds=30) == 16
    with pytest.raises(ValueError):
        SharedRequestPacer(tmp_path / "slow.sqlite", scope="model", interval_seconds=61)
