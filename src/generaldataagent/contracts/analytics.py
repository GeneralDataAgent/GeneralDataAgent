"""Contracts for certified metric analysis."""

from datetime import datetime
from typing import Literal

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


class NaturalLanguageMetricQuery(ContractModel):
    question: str = Field(min_length=1, max_length=2_000)
    metric_version: str | None = Field(default=None, pattern=r"^v[1-9][0-9]*$")
    dimensions: tuple[str, ...] = ()
    start_time: datetime | None = None
    end_time: datetime | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> "NaturalLanguageMetricQuery":
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be supplied together")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time >= self.end_time
        ):
            raise ValueError("start_time must be earlier than end_time")
        return self


class MetricCandidate(ContractModel):
    metric_name: str
    metric_version: str
    description: str
    unit: str
    matched_alias: str


class MetricResolution(ContractModel):
    status: Literal["resolved", "ambiguous", "not_found"]
    question: str
    candidates: tuple[MetricCandidate, ...] = ()
    clarification_question: str | None = None

    @model_validator(mode="after")
    def status_matches_candidates(self) -> "MetricResolution":
        if self.status == "resolved" and len(self.candidates) != 1:
            raise ValueError("resolved metric requires exactly one candidate")
        if self.status == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous metric requires multiple candidates")
        if self.status == "not_found" and self.candidates:
            raise ValueError("not_found metric cannot include candidates")
        if self.status != "resolved" and not self.clarification_question:
            raise ValueError("unresolved metric requires a clarification question")
        return self
