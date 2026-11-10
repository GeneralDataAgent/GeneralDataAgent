"""One end-to-end resource and experience observation for a completed task.

The sample keeps queue time, end-to-end latency, model calls, actual usage and
the pre-run estimate separate. Missing or incomplete measurements stay
``None``; they are never converted into zero cost.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from generaldataagent.contracts.child_budget import BudgetUsageAmount
from generaldataagent.contracts.models import ContractModel


class EstimatedResource(ContractModel):
    """Pre-run estimate; a missing field is unknown, not zero."""

    tokens: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    scan_bytes: int | None = Field(default=None, ge=0)
    cost_microunits: int | None = Field(default=None, ge=0)
    runtime_milliseconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_estimate(self) -> EstimatedResource:
        if all(
            value is None
            for value in (
                self.tokens,
                self.tool_calls,
                self.scan_bytes,
                self.cost_microunits,
                self.runtime_milliseconds,
            )
        ):
            raise ValueError("resource_estimate_empty")
        return self


class ResourceExperienceSample(ContractModel):
    task_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    passed: bool
    queue_ms: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    estimated: EstimatedResource | None = None
    measured: BudgetUsageAmount | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> ResourceExperienceSample:
        if self.latency_ms < self.queue_ms:
            raise ValueError("resource_sample_latency_before_queue")
        return self
