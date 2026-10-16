"""Versioned contracts for the governed Stage 3 data-science workbench."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ArtifactRef, ContractModel, utc_now


class SnapshotColumn(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    data_type: Literal["boolean", "integer", "number", "string"]
    role: Literal["feature", "target", "group", "timestamp", "ignored"]


class DatasetSnapshot(ContractModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    data_domain: str = Field(min_length=1, max_length=128)
    source_name: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    row_count: int = Field(ge=2)
    columns: tuple[SnapshotColumn, ...] = Field(min_length=2)
    data_artifact: ArtifactRef
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "DatasetSnapshot":
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("snapshot column names must be unique")
        if sum(column.role == "target" for column in self.columns) != 1:
            raise ValueError("snapshot requires exactly one target column")
        if not any(column.role == "feature" for column in self.columns):
            raise ValueError("snapshot requires at least one feature column")
        if self.data_artifact.task_id != self.task_id:
            raise ValueError("snapshot artifact must belong to the same task")
        if self.data_artifact.content_hash != self.content_hash:
            raise ValueError("snapshot content hash must match its data artifact")
        return self


class SplitSpec(ContractModel):
    strategy: Literal["random", "temporal", "group"] = "random"
    test_fraction: float = Field(default=0.2, ge=0.1, le=0.5)
    timestamp_column: str | None = Field(default=None, min_length=1, max_length=256)
    group_column: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def strategy_has_required_column(self) -> "SplitSpec":
        if self.strategy == "temporal" and self.timestamp_column is None:
            raise ValueError("temporal split requires timestamp_column")
        if self.strategy == "group" and self.group_column is None:
            raise ValueError("group split requires group_column")
        if self.strategy != "temporal" and self.timestamp_column is not None:
            raise ValueError("timestamp_column is only valid for temporal splits")
        if self.strategy != "group" and self.group_column is not None:
            raise ValueError("group_column is only valid for group splits")
        return self


class SandboxQuota(ContractModel):
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    memory_mb: int = Field(default=256, ge=64, le=2048)
    cpu_seconds: int = Field(default=10, ge=1, le=60)
    max_input_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    max_output_bytes: int = Field(default=1_000_000, ge=1_024, le=10_000_000)


class OCIContainerRuntime(ContractModel):
    engine: Literal["docker", "podman"]
    image_ref: str = Field(pattern=r"^[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
    cpu_count: float = Field(default=1.0, ge=0.1, le=8.0)
    pids_limit: int = Field(default=32, ge=8, le=256)
    tmpfs_mb: int = Field(default=16, ge=4, le=256)


class ExperimentSpec(ContractModel):
    experiment_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    snapshot_id: UUID
    problem_type: Literal["binary_classification", "regression"]
    target_column: str = Field(min_length=1, max_length=256)
    feature_columns: tuple[str, ...] = Field(min_length=1)
    subgroup_columns: tuple[str, ...] = Field(default=(), max_length=10)
    minimum_subgroup_rows: int = Field(default=5, ge=2, le=10_000)
    baseline: Literal["majority", "mean"]
    primary_metric: Literal["accuracy", "precision", "recall", "f1", "mae", "rmse", "r2"]
    split: SplitSpec = Field(default_factory=SplitSpec)
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    quota: SandboxQuota = Field(default_factory=SandboxQuota)

    @model_validator(mode="after")
    def validate_experiment(self) -> "ExperimentSpec":
        if self.target_column in self.feature_columns:
            raise ValueError("target column cannot also be a feature")
        if len(self.feature_columns) != len(set(self.feature_columns)):
            raise ValueError("experiment feature columns must be unique")
        if len(self.subgroup_columns) != len(set(self.subgroup_columns)):
            raise ValueError("experiment subgroup columns must be unique")
        if self.target_column in self.subgroup_columns:
            raise ValueError("target column cannot also be a subgroup column")
        classification_metrics = {"accuracy", "precision", "recall", "f1"}
        regression_metrics = {"mae", "rmse", "r2"}
        if self.problem_type == "binary_classification":
            if self.baseline != "majority" or self.primary_metric not in classification_metrics:
                raise ValueError(
                    "classification requires majority baseline and classification metric"
                )
        elif self.baseline != "mean" or self.primary_metric not in regression_metrics:
            raise ValueError("regression requires mean baseline and regression metric")
        return self


class ScienceCheck(ContractModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    passed: bool
    severity: Literal["warning", "error"]
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)


class MetricValue(ContractModel):
    name: Literal["accuracy", "precision", "recall", "f1", "mae", "rmse", "r2"]
    value: float


class SubgroupEvaluation(ContractModel):
    column: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=512)
    row_count: int = Field(ge=1)
    metrics: tuple[MetricValue, ...] = Field(min_length=1)


class ExperimentReport(ContractModel):
    experiment_id: UUID
    snapshot_id: UUID
    runtime_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    template_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    train_rows: int = Field(ge=1)
    test_rows: int = Field(ge=1)
    metrics: tuple[MetricValue, ...] = Field(min_length=1)
    eda: dict[str, object]
    statistical_checks: dict[str, object]
    error_analysis: dict[str, object]
    subgroups: tuple[SubgroupEvaluation, ...] = ()
    checks: tuple[ScienceCheck, ...]
    passed: bool
    output_artifact: ArtifactRef
    completed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "ExperimentReport":
        expected = not any(not check.passed and check.severity == "error" for check in self.checks)
        if self.passed != expected:
            raise ValueError("experiment status must match error-severity checks")
        if len({metric.name for metric in self.metrics}) != len(self.metrics):
            raise ValueError("experiment metrics must be unique")
        return self


class TrackedExperiment(ContractModel):
    experiment_id: UUID
    task_id: UUID
    snapshot_id: UUID
    spec: ExperimentSpec
    report: ExperimentReport
    recorded_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_bindings(self) -> "TrackedExperiment":
        if self.spec.experiment_id != self.experiment_id:
            raise ValueError("tracked experiment id must match its spec")
        if self.report.experiment_id != self.experiment_id:
            raise ValueError("tracked experiment id must match its report")
        if self.spec.task_id != self.task_id:
            raise ValueError("tracked experiment task must match its spec")
        if self.spec.snapshot_id != self.snapshot_id or self.report.snapshot_id != self.snapshot_id:
            raise ValueError("tracked experiment snapshot bindings must match")
        return self


class ModelCard(ContractModel):
    model_name: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=64)
    experiment_id: UUID
    snapshot_id: UUID
    intended_use: str = Field(min_length=1, max_length=4_000)
    out_of_scope_uses: tuple[str, ...] = Field(min_length=1)
    training_data: str = Field(min_length=1, max_length=4_000)
    evaluation_data: str = Field(min_length=1, max_length=4_000)
    subgroup_evaluation: str = Field(min_length=1, max_length=4_000)
    metrics: tuple[MetricValue, ...] = Field(min_length=1)
    ethical_considerations: tuple[str, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)
    monitoring_requirements: tuple[str, ...] = Field(min_length=1)
    owner: str = Field(min_length=1, max_length=256)
    approved_for_deployment: Literal[False] = False
    created_at: datetime = Field(default_factory=utc_now)


class ModelRegistrationDraft(ContractModel):
    draft_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    model_card: ModelCard
    model_artifact: ArtifactRef
    status: Literal["pending_review", "approved_draft"] = "pending_review"
    approval_id: UUID | None = None
    deployment_permitted: Literal[False] = False
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def approval_matches_status(self) -> "ModelRegistrationDraft":
        if self.model_artifact.task_id != self.task_id:
            raise ValueError("model artifact must belong to the registration task")
        if self.status == "pending_review" and self.approval_id is not None:
            raise ValueError("pending registration draft cannot carry consumed approval")
        if self.status == "approved_draft" and self.approval_id is None:
            raise ValueError("approved registration draft requires approval evidence")
        return self
