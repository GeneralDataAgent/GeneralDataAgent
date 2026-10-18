"""Versioned downstream drafts; no arbitrary commands, URLs, or deployment actions."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from generaldataagent.contracts.models import ContractModel, utc_now

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class DashboardDraft(ContractModel):
    kind: Literal["dashboard"] = "dashboard"
    title: str = Field(min_length=1, max_length=200)
    metric_names: tuple[str, ...] = Field(min_length=1, max_length=20)
    evidence_hashes: tuple[Digest, ...] = Field(min_length=1, max_length=20)


class TicketDraft(ContractModel):
    kind: Literal["ticket"] = "ticket"
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"


class SchedulePRDraft(ContractModel):
    kind: Literal["schedule_pr"] = "schedule_pr"
    pipeline: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    interval_minutes: int = Field(ge=15, le=525600)
    rationale: str = Field(min_length=1, max_length=2000)
    rollback_interval_minutes: int = Field(ge=15, le=525600)


class RegistrationDraft(ContractModel):
    kind: Literal["model_registration"] = "model_registration"
    experiment_id: UUID
    model_card_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deployment_allowed: Literal[False] = False


class NotificationDraft(ContractModel):
    kind: Literal["notification"] = "notification"
    message: str = Field(min_length=1, max_length=2000)
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReportDraft(ContractModel):
    kind: Literal["report"] = "report"
    title: str = Field(min_length=1, max_length=200)
    report_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    period: str = Field(min_length=1, max_length=128)


DraftPayload = Annotated[
    DashboardDraft
    | TicketDraft
    | SchedulePRDraft
    | RegistrationDraft
    | NotificationDraft
    | ReportDraft,
    Field(discriminator="kind"),
]
ActionKind = Literal[
    "dashboard", "ticket", "schedule_pr", "model_registration", "notification", "report"
]


class ActionIntent(ContractModel):
    action_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    thread_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    destination: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    expected_revision: int = Field(default=0, ge=0)
    payload: DraftPayload
    operation: Literal["deliver", "compensate"] = "deliver"
    compensates: UUID | None = None
    expires_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ActionIntent":
        if self.expires_at <= self.created_at:
            raise ValueError("action must expire after creation")
        if (self.operation == "compensate") != (self.compensates is not None):
            raise ValueError("compensation requires an original action")
        return self


class ActionReceipt(ContractModel):
    action_id: UUID
    intent_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination: str
    revision: int = Field(ge=1)
    operation: Literal["deliver", "compensate"]
    backend: Literal["local_draft"] = "local_draft"
    external_delivery: Literal[False] = False
    recorded_at: AwareDatetime = Field(default_factory=utc_now)
