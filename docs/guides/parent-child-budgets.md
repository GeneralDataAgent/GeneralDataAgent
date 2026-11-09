# Parent/child budgets

Stage 15.1B provides a deterministic accounting library for a parent task to reserve a bounded
subtask budget, reconcile complete provider/tool measurements, and represent gaps explicitly.

## Use

```python
from generaldataagent.contracts.child_budget import (
    BudgetReservationAmount,
    BudgetUsageAmount,
)
from generaldataagent.routing.child_budgets import ChildBudgetBook

book = ChildBudgetBook(path)
reservation = book.reserve(parent_task_id, "child-a", "res-a", BudgetReservationAmount(tokens=100, tool_calls=10))
settlement = book.settle(
    parent_task_id,
    "child-a",
    reservation.reservation_digest,
    BudgetUsageAmount(
        source="provider_reported",
        tokens=40,
        tool_calls=3,
        scan_bytes=400,
        cost_microunits=40,
        runtime_milliseconds=1000,
        complete=True,
    ),
    retry_count=1,
)
```

`ChildBudgetBook` is not connected to route selection, task creation, or a worker runtime. The
parent coordinator remains responsible for allowing children. Partial measurements use
`record_telemetry()`; their missing fields stay `None`, and they cannot settle. Release, fail,
unknown, and cancel return a terminal state appropriate to the operation.

## Fail-closed behavior

The following are refused `ChildBudgetRefused`, not downgraded:

- derived usage above a reservation bound
- incomplete usage submitted for settlement
- an unknown source used with any numeric usage
- digest or parent-task mismatch
- a competing reservation for the same child
- reuse after settle, release, fail, unknown, or cancel
- an active reservation state that cannot be closed

This does not change production admission, spend estimation, concurrency, or worker execution.
