"""Pydantic models used at every system boundary."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from generaldataagent.contracts.types import (
    AgentRole,
    ArtifactClassification,
    RiskLevel,
    TaskStatus,
    ValidationStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Strict immutable base for serialized system contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)
    schema_version: str = "1.0"


class Budget(ContractModel):
    max_runtime_seconds: int = Field(default=600, ge=1, le=86_400)
    max_scan_bytes: int = Field(default=10 * 1024**3, ge=0)
    max_tokens: int = Field(default=30_000, ge=1)
    max_tool_calls: int = Field(default=30, ge=1, le=1_000)
    max_subtasks: int = Field(default=20, ge=0, le=100)


class ArtifactRef(ContractModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    uri: str = Field(pattern=r"^artifact://")
    artifact_type: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    classification: ArtifactClassification = ArtifactClassification.INTERNAL
    created_by: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)
    lineage: tuple[UUID, ...] = ()
    size_bytes: int = Field(ge=0)


class TaskStep(ContractModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    objective: str = Field(min_length=1, max_length=2_000)
    agent_role: AgentRole
    depends_on: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.R1


class TaskPlan(ContractModel):
    task_id: UUID = Field(default_factory=uuid4)
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=4_000)
    status: TaskStatus = TaskStatus.PLANNED
    data_domains: tuple[str, ...] = ()
    read_only: bool = True
    budget: Budget = Field(default_factory=Budget)
    steps: tuple[TaskStep, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_dag(self) -> "TaskPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("task step ids must be unique")

        known: set[str] = set()
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(
                    f"step {step.step_id!r} has unknown or forward dependencies: {sorted(unknown)}"
                )
            if step.step_id in step.depends_on:
                raise ValueError(f"step {step.step_id!r} cannot depend on itself")
            known.add(step.step_id)
        return self


class ValidationCheck(ContractModel):
    check_id: str = Field(min_length=1, max_length=128)
    passed: bool
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    evidence: tuple[ArtifactRef, ...] = ()


class ValidationResult(ContractModel):
    task_id: UUID
    status: ValidationStatus
    checks: tuple[ValidationCheck, ...]
    verifier: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "ValidationResult":
        any_failed = any(not check.passed for check in self.checks)
        if self.status == ValidationStatus.PASS and any_failed:
            raise ValueError("validation cannot pass while a check has failed")
        if self.status == ValidationStatus.FAIL and not any_failed:
            raise ValueError("failed validation requires at least one failed check")
        return self


class ToolRequest(ContractModel):
    request_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    agent_role: AgentRole
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=1_024)
    arguments: dict[str, Any] = Field(default_factory=dict)
    data_domains: tuple[str, ...] = ()
    requested_risk: RiskLevel = RiskLevel.R1


class PolicyDecision(ContractModel):
    request_id: UUID
    decision: Literal["allow", "deny", "require_approval"]
    risk_level: RiskLevel
    reason_code: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2_000)
    obligations: tuple[str, ...] = ()
    decided_at: datetime = Field(default_factory=utc_now)


class AuditEvent(ContractModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1, max_length=128)
    task_id: UUID
    actor_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
