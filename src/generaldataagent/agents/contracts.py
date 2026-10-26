"""Structured contracts exchanged by the metric-analysis agent team."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from generaldataagent.contracts import ExecutionReceipt


class SpecialistResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["discovery", "data-analyst", "verifier", "reporter"]
    status: Literal["completed", "failed", "needs_human"]
    summary: str = Field(min_length=1, max_length=2_000)
    task_id: str | None = None
    artifact_uris: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class SupervisorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed", "needs_human"]
    summary: str = Field(min_length=1, max_length=2_000)
    task_id: str | None = None
    delegated_roles: tuple[Literal["discovery", "data-analyst", "verifier", "reporter"], ...]
    artifact_uris: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    receipts: tuple[ExecutionReceipt, ...] = ()
