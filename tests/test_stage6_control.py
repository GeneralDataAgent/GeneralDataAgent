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


@pytest.mark.parametrize("seconds", [90, 120, 150, 180])
def test_paced_model_uses_the_case_budget_without_resetting_it_on_bind(monkeypatch, seconds):
    import time

    from generaldataagent.evaluation.stage_six_control import PacedModel

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    waits = []

    class Pacer:
        def reserve(self, **kwargs):
            waits.append(kwargs["max_wait_seconds"])
            return 0

    class Model:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return "scripted response"

    original = PacedModel(Model(), Pacer(), max_seconds=seconds)
    now[0] += 20
    bound = original.bind(tools=[{"name": "test"}])
    assert bound.deadline == original.deadline
    assert bound.invoke([]) == "scripted response"
    # Preserve the existing two five-second margins, but apply them to this
    # case's budget. Binding must not restart the countdown.
    assert waits == [seconds - 20 - 10]


@pytest.mark.parametrize("elapsed", [110, 111, 121])
def test_expired_pacing_budget_neither_reserves_a_slot_nor_calls_model(monkeypatch, elapsed):
    import time

    from generaldataagent.evaluation.stage_six_control import PacedModel, PacingDeadlineExceeded

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    calls = []

    class Pacer:
        def reserve(self, **kwargs):
            calls.append("reserve")
            return 0

    class Model:
        def invoke(self, messages):
            calls.append("model")
            return "response"

    model = PacedModel(Model(), Pacer())
    now[0] += elapsed
    with pytest.raises(PacingDeadlineExceeded):
        model.invoke([])
    assert calls == []


@pytest.mark.parametrize("phase", ["reserve", "sleep"])
def test_pacing_rechecks_deadline_after_reservation_and_wait(monkeypatch, phase):
    import time

    from generaldataagent.evaluation.stage_six_control import PacedModel, PacingDeadlineExceeded

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    calls = []

    class Pacer:
        def reserve(self, **kwargs):
            calls.append("reserve")
            if phase == "reserve":
                now[0] += 110
                return 0
            return 2

    def oversleep(delay):
        assert delay == 2
        calls.append("sleep")
        now[0] += 110

    class Model:
        def invoke(self, messages):
            calls.append("model")
            return "response"

    monkeypatch.setattr(time, "sleep", oversleep)
    model = PacedModel(Model(), Pacer())
    with pytest.raises(PacingDeadlineExceeded):
        model.invoke([])
    assert calls == (["reserve"] if phase == "reserve" else ["reserve", "sleep"])


def test_pacing_still_invokes_once_when_a_reserved_wait_finishes_in_time(monkeypatch):
    import time

    from generaldataagent.evaluation.stage_six_control import PacedModel

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    calls = []

    class Pacer:
        def reserve(self, **kwargs):
            assert kwargs["max_wait_seconds"] == 110
            calls.append("reserve")
            return 2

    def sleep(delay):
        calls.append("sleep")
        now[0] += delay

    class Model:
        def invoke(self, messages):
            calls.append("model")
            return "response"

    monkeypatch.setattr(time, "sleep", sleep)
    assert PacedModel(Model(), Pacer()).invoke([]) == "response"
    assert calls == ["reserve", "sleep", "model"]


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf")])
def test_paced_model_refuses_invalid_case_budgets(seconds):
    from generaldataagent.evaluation.stage_six_control import PacedModel

    with pytest.raises(ValueError, match="invalid pacing time budget"):
        PacedModel(object(), object(), max_seconds=seconds)


@pytest.mark.parametrize("seconds", [90, 120, 150, 180])
def test_worker_passes_its_case_budget_to_the_paced_model(monkeypatch, capsys, seconds):
    import io
    import json
    import time

    import generaldataagent.evaluation.stage_six_control as control
    import generaldataagent.evaluation.stage_six_worker as worker

    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(
        worker,
        "resolve_evaluation_model",
        lambda **kwargs: (object(), {"provider_id": "scripted", "endpoint_hash": "synthetic"}),
    )
    monkeypatch.setattr(worker, "build_evaluation_model", lambda *args: object())
    monkeypatch.setattr(control, "SharedRequestPacer", lambda *args, **kwargs: object())

    class Agent:
        def __init__(self, model, **kwargs):
            assert kwargs["max_seconds"] == seconds
            assert model.deadline == 1000.0 + seconds - 5

        def run(self, *args, **kwargs):
            return {"tokens": 0}

    monkeypatch.setattr(worker, "BoundedSQLAgent", Agent)
    payload = {
        "app_type": "scripted",
        "provider_id": "scripted",
        "request_interval_seconds": 2,
        "pacing_database": "unused.sqlite",
        "max_seconds": seconds,
        "task": {
            "benchmark": "synthetic",
            "case_id": "pacing",
            "question": "q",
            "instructions": "",
            "difficulty": "easy",
            "asset": "unused",
        },
        "database": "unused.sqlite",
        "documents": "",
    }
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    worker.main()
    assert json.loads(capsys.readouterr().out) == {"tokens": 0}


def test_paced_model_binding_preserves_shared_pacer_and_original_deadline():
    from generaldataagent.evaluation.stage_six_control import PacedModel

    class Pacer:
        def __init__(self):
            self.calls = 0

        def reserve(self, **kwargs):
            self.calls += 1
            return 0

    class Model:
        def bind(self, **kwargs):
            self.options = kwargs
            return self

        def invoke(self, messages):
            return self.options

    pacer = Pacer()
    original = PacedModel(Model(), pacer)
    bound = original.bind(tools=[{"name": "test"}])
    assert bound.pacer is pacer and bound.deadline == original.deadline
    assert bound.invoke([]) == {"tools": [{"name": "test"}]}
    assert pacer.calls == 1
