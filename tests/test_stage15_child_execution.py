"""Stage 15.1C checks bounded child concurrency in one atomic parent budget."""

import threading
from dataclasses import dataclass
from uuid import uuid4

import pytest

from generaldataagent.contracts.child_budget import BudgetReservationAmount, BudgetUsageAmount
from generaldataagent.contracts.child_execution import GovernorJob
from generaldataagent.routing.child_budgets import ChildBudgetBook, ChildBudgetRefused
from generaldataagent.routing.child_execution import ChildExecutionGovernor, ChildExecutionRefused


@dataclass(frozen=True)
class ToolOutput:
    tokens: int | None
    scan_bytes: int | None
    cost_microunits: int | None


def reservation(runtime_ms: int = 1000) -> BudgetReservationAmount:
    return BudgetReservationAmount(
        tokens=100,
        tool_calls=2,
        scan_bytes=1000,
        cost_microunits=100,
        runtime_milliseconds=runtime_ms,
    )


def governor(tmp_path):
    return ChildExecutionGovernor(ChildBudgetBook(tmp_path / "child-budgets"))


def job(
    *,
    run_fn,
    admission_fn=None,
    timeout_ms=1000,
    deadline_ms=1000,
    reserve_fn=None,
    cancellation=None,
    children=("child-a", "child-b"),
):
    return GovernorJob(
        job_id=uuid4(),
        stage_id="stage",
        children=children,
        deadline_ms=deadline_ms,
        timeout_ms=timeout_ms,
        reserve_fn=reserve_fn or (lambda child: reservation(runtime_ms=timeout_ms)),
        run_fn=run_fn,
        admission_fn=admission_fn,
        cancellation=cancellation,
    )


def output(child, *, cost=40):
    return ToolOutput(tokens=40, scan_bytes=400, cost_microunits=cost)


def test_bounded_concurrency_runs_two_children_concurrently(tmp_path):
    child_governor = governor(tmp_path)
    barrier = threading.Barrier(2)

    def run(parent, child, grant):
        barrier.wait(timeout=1)
        return output(child, cost=None)

    current = job(run_fn=run)
    result = child_governor.run(current)

    assert result.status == "completed"
    assert sorted(result.results) == ["child-a", "child-b"]
    for child in ("child-a", "child-b"):
        state = child_governor.book.get(current.job_id, child)
        assert state is not None
        assert state.status == "telemetry_recorded"
        assert state.usage.cost_microunits is None
        assert state.usage.complete is False


def test_completed_stage_settles_all_children_atomically(tmp_path):
    child_governor = governor(tmp_path)
    current = job(run_fn=lambda *args: output(args[1]))
    child_governor.run(current)
    partial = BudgetUsageAmount(
        source="tool_reported",
        tokens=40,
        tool_calls=1,
        scan_bytes=400,
        cost_microunits=40,
        runtime_milliseconds=10,
        complete=False,
    )
    with pytest.raises(ChildExecutionRefused):
        child_governor.settle(current, usage={"child-a": partial})

    complete = partial.model_copy(update={"complete": True})
    settlement = child_governor.settle(current, usage={"child-a": complete, "child-b": complete})
    assert settlement["child-a"].status == "settled"
    assert settlement["child-b"].status == "settled"
    assert child_governor.book.get(current.job_id, "child-a").status == "settled"


def test_partial_failure_closes_known_and_unknown_usage(tmp_path):
    child_governor = governor(tmp_path)
    finished = threading.Event()

    def run(parent, child, grant):
        if child == "child-b":
            assert finished.wait(1)
            raise RuntimeError("tool failed")
        value = output(child)
        finished.set()
        return value

    current = job(run_fn=run)
    with pytest.raises(ChildExecutionRefused):
        child_governor.run(current)

    known = child_governor.book.get(current.job_id, "child-a")
    failed = child_governor.book.get(current.job_id, "child-b")
    assert known is not None and known.status == "failed"
    assert known.usage.source == "tool_reported"
    assert failed is not None and failed.status == "failed"
    assert failed.usage.source == "unknown"
    with pytest.raises(ChildExecutionRefused):
        child_governor.settle(
            current,
            usage={
                "child-a": BudgetUsageAmount(
                    source="provider_reported",
                    tokens=1,
                    tool_calls=1,
                    scan_bytes=1,
                    cost_microunits=1,
                    runtime_milliseconds=1,
                    complete=True,
                ),
                "child-b": BudgetUsageAmount(
                    source="provider_reported",
                    tokens=1,
                    tool_calls=1,
                    scan_bytes=1,
                    cost_microunits=1,
                    runtime_milliseconds=1,
                    complete=True,
                ),
            },
        )


def test_timeout_closes_stage_and_discards_late_worker(tmp_path):
    child_governor = governor(tmp_path)
    gate = threading.Event()

    def run(parent, child, grant):
        if child == "child-b":
            gate.wait(1)
        return output(child)

    current = job(
        run_fn=run,
        timeout_ms=20,
        reserve_fn=lambda child: reservation(runtime_ms=20),
    )
    with pytest.raises(ChildExecutionRefused):
        child_governor.run(current)
    gate.set()

    assert child_governor.book.get(current.job_id, "child-a").status == "failed"
    timed_out = child_governor.book.get(current.job_id, "child-b")
    assert timed_out is not None and timed_out.status == "failed"
    assert timed_out.usage.source == "unknown"


def test_cancellation_event_closes_stage_and_discards_late_worker(tmp_path):
    child_governor = governor(tmp_path)
    cancellation = threading.Event()
    gate = threading.Event()

    def run(parent, child, grant):
        if child == "child-b":
            cancellation.set()
            return output(child)
        gate.wait(1)
        return output(child)

    current = job(run_fn=run, cancellation=cancellation)
    with pytest.raises(ChildExecutionRefused):
        child_governor.run(current)
    gate.set()

    assert child_governor.book.get(current.job_id, "child-a").state == "cancelled"
    assert child_governor.book.get(current.job_id, "child-b").state == "cancelled"


def test_admission_revocation_refuses_before_reservation(tmp_path):
    child_governor = governor(tmp_path)
    current = job(
        run_fn=lambda *args: output(args[1]),
        admission_fn=lambda child: False,
    )
    with pytest.raises(ChildExecutionRefused):
        child_governor.run(current)
    assert child_governor.book.get(current.job_id, "child-a") is None
    assert child_governor.book.get(current.job_id, "child-b") is None


def test_reservation_over_timeout_refuses_before_execution(tmp_path):
    child_governor = governor(tmp_path)
    current = job(
        run_fn=lambda *args: output(args[1]),
        reserve_fn=lambda child: reservation(runtime_ms=2000),
    )
    with pytest.raises(ChildExecutionRefused):
        child_governor.run(current)
    assert child_governor.book.get(current.job_id, "child-a") is None


def test_three_children_are_refused_before_execution():
    with pytest.raises(ValueError, match="governor_job_invalid"):
        GovernorJob(
            job_id=uuid4(),
            stage_id="stage",
            children=("child-a", "child-b", "child-c"),
            deadline_ms=1000,
            timeout_ms=1000,
            reserve_fn=lambda child: reservation(),
            run_fn=lambda *args: output(args[1]),
        )


def test_deadline_shorter_than_timeout_is_refused():
    with pytest.raises(ValueError, match="governor_timeout_exceeds_deadline"):
        GovernorJob(
            job_id=uuid4(),
            stage_id="stage",
            children=("child-a",),
            deadline_ms=0,
            timeout_ms=1,
            reserve_fn=lambda child: reservation(),
            run_fn=lambda *args: output(args[1]),
        )


def test_stage_reuse_after_settlement_is_refused(tmp_path):
    child_governor = governor(tmp_path)
    current = job(run_fn=lambda *args: output(args[1]))
    child_governor.run(current)
    complete = BudgetUsageAmount(
        source="provider_reported",
        tokens=40,
        tool_calls=1,
        scan_bytes=400,
        cost_microunits=40,
        runtime_milliseconds=10,
        complete=True,
    )
    child_governor.settle(current, usage={"child-a": complete, "child-b": complete})
    with pytest.raises(ChildBudgetRefused):
        child_governor.book.reserve(current.job_id, "child-a", "new-res", reservation())
