"""Contracts for certified metric analysis."""

from datetime import datetime

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel


class MetricQuery(ContractModel):
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    metric_version: str = Field(pattern=r"^v[1-9][0-9]*$")
    dimensions: tuple[str, ...] = ()
    start_time: datetime | None = None
    end_time: datetime | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> "MetricQuery":
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be supplied together")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time >= self.end_time
        ):
            raise ValueError("start_time must be earlier than end_time")
        return self


class ActorContext(ContractModel):
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    data_domains: tuple[str, ...] = Field(min_length=1)
