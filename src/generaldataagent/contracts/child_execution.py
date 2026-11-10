"""A bounded, frozen definition of one read-only child stage.

The governor treats this as immutable domain data. It cannot create a tool,
admit a subagent, or expand the child set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from uuid import UUID

from generaldataagent.contracts.child_budget import BudgetReservationAmount


@dataclass(frozen=True, slots=True)
class GovernorJob:
    job_id: UUID
    stage_id: str
    children: tuple[object, ...]
    deadline_ms: int
    timeout_ms: int
    reserve_fn: Callable[[object], BudgetReservationAmount]
    run_fn: Callable[[UUID, object, object], object]
    admission_fn: Callable[[object], bool] | None = None
    cancellation: Event | None = None

    def __post_init__(self):
        if (
            type(self.job_id) is not UUID
            or type(self.stage_id) is not str
            or not self.stage_id
            or type(self.children) is not tuple
            or not 1 <= len(self.children) <= 2
            or any(type(child) is not str for child in self.children)
            or len({child for child in self.children}) != len(self.children)
            or type(self.deadline_ms) is not int
            or self.deadline_ms < 0
            or type(self.timeout_ms) is not int
            or self.timeout_ms <= 0
            or not callable(self.reserve_fn)
            or not callable(self.run_fn)
            or (self.admission_fn is not None and not callable(self.admission_fn))
            or (self.cancellation is not None and not isinstance(self.cancellation, Event))
        ):
            raise ValueError("governor_job_invalid")
        if self.timeout_ms > self.deadline_ms:
            raise ValueError("governor_timeout_exceeds_deadline")
