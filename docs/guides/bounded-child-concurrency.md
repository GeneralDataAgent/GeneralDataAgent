# Bounded child concurrency

Stage 15.1C exposes a deterministic child-execution governor for one or two independent,
read-only children at one fixed depth. It does not select a route, register a tool, or expose an
API. It runs children only through caller-supplied callables.

```python
from pathlib import Path

from generaldataagent.contracts.child_budget import BudgetReservationAmount
from generaldataagent.contracts.child_execution import GovernorJob
from generaldataagent.routing.child_budgets import ChildBudgetBook
from generaldataagent.routing.child_execution import ChildExecutionGovernor

governor = ChildExecutionGovernor(ChildBudgetBook(Path("state/child-budgets")))


def reserve(child):
    return BudgetReservationAmount(tokens=100, tool_calls=1, runtime_milliseconds=5_000)


job = GovernorJob(
    job_id=task_id,
    stage_id="analyse",
    children=("population", "quality"),
    deadline_ms=10_000,
    timeout_ms=5_000,
    reserve_fn=reserve,
    run_fn=run_read_only_child,
)
outcome = governor.run(job)
settlements = governor.settle(job, usage=complete_measured_usage)
```

The governor validates the fixed child list and an optional admission predicate, reserves every
child in one transaction, and starts one daemon worker thread per child. Each child is bounded by
`min(timeout_ms, reservation.runtime_milliseconds)` and the whole stage by `deadline_ms`. The
successful outcome is a `StageOutcome` containing results, partial usage and committed child
states; successful children are left `telemetry_recorded`, not settled.

Telemetry is derived from the returned value's `tokens`, `scan_bytes` and `cost_microunits`
attributes. A missing value stays `None`, so the usage remains incomplete; this is not a zero
cost. An observed value above the reservation, an elapsed runtime above the reservation, a
worker exception or a timeout fails the stage.

A failure closes every child of the stage as `failed` in one transaction, preserving measured
usage for children that finished and recording `unknown` for the rest. Parent cancellation or a
stage deadline closes them as `cancelled`. Both cases raise `ChildExecutionRefused`, and results
that arrive from a worker after the close are discarded. Settlement is a separate explicit call
with complete usage for every child of a stage whose states are all `telemetry_recorded`.

This control is in-process: one governor lock and one SQLite connection per mutation. It is not a
worker runtime, a lease-fenced queue, a route chooser, or a production admission change. A
multi-process deployment would need a lease or fencing mechanism that this slice does not
provide.
