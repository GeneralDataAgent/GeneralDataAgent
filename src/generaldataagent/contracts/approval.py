"""Contracts for explicit, request-bound human approvals."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.models import ContractModel, utc_now
from generaldataagent.contracts.types import ArtifactClassification


class ApprovalRequest(ContractModel):
    approval_id: UUID = Field(default_factory=uuid4)
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_id: UUID
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=1_024)
    parameters: dict[str, Any] = Field(default_factory=dict)
    data_domains: tuple[str, ...] = Field(default=(), max_length=100)
    impact_scope: str = Field(min_length=1, max_length=2_000)
    cost_estimate: str = Field(min_length=1, max_length=1_000)
    classification: ArtifactClassification
    rollback_plan: str = Field(min_length=1, max_length=4_000)
    expires_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def expires_after_creation(self) -> "ApprovalRequest":
        if self.expires_at <= self.created_at:
            raise ValueError("approval request must expire after creation")
        return self


class ApprovalGrant(ContractModel):
    approval_id: UUID
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approver_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    approved_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def expires_after_approval(self) -> "ApprovalGrant":
        if self.expires_at <= self.approved_at:
            raise ValueError("approval grant must expire after approval")
        return self


class ApprovalDisposition(ContractModel):
    approval_id: UUID
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    disposition: Literal["rejected", "cancelled"]
    actor_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2_000)
    recorded_at: AwareDatetime = Field(default_factory=utc_now)


class ApprovalRecord(ContractModel):
    request: ApprovalRequest
    grant: ApprovalGrant | None = None
    disposition: ApprovalDisposition | None = None
    consumed: bool = False


ApprovalState = Literal["pending", "approved", "rejected", "cancelled", "consumed", "expired"]


class ApprovalStatus(ContractModel):
    """Requester-scoped projection: exact target facts, no parameters or rollback text."""

    approval_id: UUID
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_id: UUID
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=1_024)
    data_domains: tuple[str, ...] = Field(default=(), max_length=100)
    classification: ArtifactClassification
    impact_scope: str = Field(min_length=1, max_length=2_000)
    cost_estimate: str = Field(min_length=1, max_length=1_000)
    state: ApprovalState
    created_at: AwareDatetime
    expires_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def decision_matches_state(self) -> "ApprovalStatus":
        if self.expires_at <= self.created_at:
            raise ValueError("approval status must expire after creation")
        decided = self.decided_at is not None and self.decided_by is not None
        if (self.decided_at is None) != (self.decided_by is None):
            raise ValueError("approval decision must record both time and actor")
        if self.state in {"rejected", "cancelled"}:
            if not decided or self.reason is None:
                raise ValueError("closed approval must record decision and reason")
        elif self.state in {"approved", "consumed"}:
            if not decided or self.reason is not None:
                raise ValueError("approved approval must record only its decision")
        else:
            if self.reason is not None:
                raise ValueError("pending or expired approval cannot carry a reason")
            if self.state == "pending" and decided:
                raise ValueError("pending approval cannot record a decision")
        return self


class ApprovalStatusPage(ContractModel):
    items: tuple[ApprovalStatus, ...] = Field(default=(), max_length=100)
    next_cursor: int | None = Field(default=None, ge=1)
