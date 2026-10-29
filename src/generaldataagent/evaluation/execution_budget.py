"""One immutable resource declaration for protocols and isolated execution."""

import math
from dataclasses import dataclass

DEADLINE_MARGIN_SECONDS = 5
DEFAULT_WORKER_SECONDS = 120
DEFAULT_WORKER_STEPS = 6
DEFAULT_WORKER_REPAIRS = 3


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_steps: int = DEFAULT_WORKER_STEPS
    max_seconds: int = DEFAULT_WORKER_SECONDS
    max_repairs: int = DEFAULT_WORKER_REPAIRS
    max_tokens: int | None = None
    max_output_tokens: int | None = None
    deadline_seconds: int | float | None = None

    def __post_init__(self):
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens < 32
        ):
            raise ValueError("invalid per-call output token budget")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("invalid total observed token budget")
        if type(self.max_seconds) is not int or self.max_seconds < 1:
            raise ValueError("invalid worker time budget")
        if self.deadline_seconds is not None and (
            type(self.deadline_seconds) not in {int, float}
            or not math.isfinite(self.deadline_seconds)
        ):
            raise ValueError("invalid process deadline")
        if self.process_deadline <= self.max_seconds:
            raise ValueError("process deadline must exceed the worker time budget")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("invalid declared step budget")
        if type(self.max_repairs) is not int or self.max_repairs < 0:
            raise ValueError("repair budget must be a non-negative integer")

    @property
    def process_deadline(self) -> int | float:
        return (
            self.max_seconds + DEADLINE_MARGIN_SECONDS
            if self.deadline_seconds is None
            else self.deadline_seconds
        )

    def worker_payload(self) -> dict:
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_repairs": self.max_repairs,
            "max_tokens": self.max_tokens,
            "max_output_tokens": self.max_output_tokens,
        }

    def worker_kwargs(self) -> dict:
        return {**self.worker_payload(), "deadline_seconds": self.process_deadline}

    def protocol_kwargs(self) -> dict:
        return {
            "steps": self.max_steps,
            "seconds": self.max_seconds,
            "max_tokens": self.max_tokens,
            "max_output_tokens": self.max_output_tokens,
            "hard_case_deadline": self.process_deadline,
        }
