"""Versioned downstream drafts; no arbitrary commands, URLs, or deployment actions."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from generaldataagent.contracts.analytics import MetricQuery
from generaldataagent.contracts.delivery import DeliveryPackage
from generaldataagent.contracts.models import ArtifactRef, ContractModel, utc_now

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


class ScheduledReport(ContractModel):
    schedule_id: UUID
    index: int = Field(ge=0, le=99)
    query: MetricQuery
    delivery: DeliveryPackage


class ReportDraft(ContractModel):
    kind: Literal["report"] = "report"
    title: str = Field(min_length=1, max_length=200)
    report_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    period: str = Field(min_length=1, max_length=128)
    report_artifact: ArtifactRef | None = None
    publication_text: str | None = Field(default=None, min_length=1, max_length=12000)

    @model_validator(mode="after")
    def report_hash_matches_artifact(self):
        if self.publication_text is not None and self.report_artifact is None:
            raise ValueError("report publication requires a frozen artifact")
        if self.report_artifact is not None and (
            self.report_artifact.content_hash != self.report_hash
            or self.report_artifact.artifact_type != "scheduled_report"
        ):
            raise ValueError("report draft must match its frozen report artifact")
        return self


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


class FeishuBinding(ContractModel):
    backend: Literal["feishu"] = "feishu"
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9]{1,64}$")
    sender_id: str = Field(pattern=r"^ou_[A-Za-z0-9]{1,128}$")
    identity: Literal["bot", "user"]
    receiver_type: Literal["chat_id", "open_id"]
    receiver_id: str = Field(pattern=r"^(oc|ou)_[A-Za-z0-9]{1,128}$")
    wire_content_hash: Digest
    configuration_hash: Digest


class ExternalMessageReceipt(ContractModel):
    backend: Literal["feishu"] = "feishu"
    message_id: str = Field(pattern=r"^om_[A-Za-z0-9]{1,128}$")
    chat_id: str = Field(pattern=r"^oc_[A-Za-z0-9]{1,128}$")
    sender_id: str = Field(pattern=r"^ou_[A-Za-z0-9]{1,128}$")
    wire_content_hash: Digest
    configuration_hash: Digest
    state: Literal["delivered", "recalled"]

    @property
    def receiver_id(self) -> str:
        return self.message_id


GitSHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class GitHubPRBinding(ContractModel):
    backend: Literal["github"] = "github"
    configuration_hash: Digest
    repository: str
    repository_id: int = Field(gt=0)
    actor_id: int = Field(gt=0)
    base_branch: str
    base_sha: GitSHA
    branch: str
    path: str
    old_content: str = Field(min_length=1, max_length=8192)
    new_content: str = Field(min_length=1, max_length=8192)
    old_blob_sha: GitSHA
    new_blob_sha: GitSHA
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class GitHubPRReceipt(ContractModel):
    backend: Literal["github"] = "github"
    repository: str
    repository_id: int = Field(gt=0)
    actor_id: int = Field(gt=0)
    pull_number: int = Field(gt=0)
    head_sha: GitSHA
    binding_hash: Digest
    configuration_hash: Digest
    state: Literal["delivered", "recalled"]

    @property
    def receiver_id(self) -> str:
        return str(self.pull_number)


class GitHubReceiverHint(ContractModel):
    backend: Literal["github"] = "github"
    branch: str = Field(min_length=1, max_length=128)
    stage: Literal["branch_created", "commit_created", "pr_created"]
    head_sha: GitSHA | None = None
    pull_number: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def matching_stage(self):
        if self.stage == "branch_created" and (
            self.head_sha is not None or self.pull_number is not None
        ):
            raise ValueError("branch hint cannot claim a commit or PR")
        if self.stage != "branch_created" and self.head_sha is None:
            raise ValueError("commit/PR hint requires a head SHA")
        if (self.stage == "pr_created") != (self.pull_number is not None):
            raise ValueError("PR hint requires a pull number")
        return self


class GitHubReviewBinding(ContractModel):
    backend: Literal["github_review"] = "github_review"
    repository: str
    repository_id: int = Field(gt=0, strict=True)
    actor_id: int = Field(gt=0, strict=True)
    kind: Literal["ticket", "dashboard", "model_registration"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    configuration_hash: Digest


class GitHubReviewReceipt(ContractModel):
    backend: Literal["github_review"] = "github_review"
    repository: str
    repository_id: int = Field(gt=0, strict=True)
    actor_id: int = Field(gt=0, strict=True)
    issue_number: int = Field(gt=0, le=9999999999, strict=True)
    issue_id: int = Field(gt=0, strict=True)
    binding_hash: Digest
    configuration_hash: Digest
    state: Literal["delivered", "recalled"]

    @property
    def receiver_id(self) -> str:
        return str(self.issue_number)


ReceiverBinding = Annotated[
    FeishuBinding | GitHubPRBinding | GitHubReviewBinding, Field(discriminator="backend")
]
ReceiverReceipt = Annotated[
    ExternalMessageReceipt | GitHubPRReceipt | GitHubReviewReceipt, Field(discriminator="backend")
]


class ActionIntent(ContractModel):
    action_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    thread_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    destination: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    expected_revision: int = Field(default=0, ge=0)
    payload: DraftPayload
    delivery_binding: ReceiverBinding | None = None
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
    backend: Literal["local_draft", "feishu", "github", "github_review"] = "local_draft"
    external_delivery: bool = False
    external: ReceiverReceipt | None = None
    recorded_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_receiver_evidence(self) -> "ActionReceipt":
        if self.backend == "local_draft":
            if self.external_delivery or self.external is not None:
                raise ValueError("local draft cannot claim external delivery")
        elif not self.external_delivery or self.external is None:
            raise ValueError("external delivery requires a receiver receipt")
        if self.external is not None and self.external.backend != self.backend:
            raise ValueError("external receipt must match its backend")
        if self.external is not None and self.external.state != (
            "delivered" if self.operation == "deliver" else "recalled"
        ):
            raise ValueError("receiver receipt must match action operation")
        return self
