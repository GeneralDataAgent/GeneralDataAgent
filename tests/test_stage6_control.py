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
