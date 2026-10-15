"""Versioned contracts for deterministic Stage 2 data-engineering work."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel, utc_now


class ConnectorCapabilities(ContractModel):
    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    dialect: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    read_only: bool
    schema_introspection: bool
    profiling: bool
    sampling: bool
    explain: bool
    cost_estimation: bool = False
    cancellation: bool = False
    transactions: bool = False
    dml: bool = False
    ddl: bool = False

    @model_validator(mode="after")
    def read_only_excludes_mutations(self) -> "ConnectorCapabilities":
        if self.read_only and (self.dml or self.ddl):
            raise ValueError("a read-only connector cannot declare DML or DDL capabilities")
        return self


class ColumnSchema(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    data_type: str = Field(min_length=1, max_length=256)
    nullable: bool
    ordinal_position: int = Field(ge=0)


class RelationSchema(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)
    columns: tuple[ColumnSchema, ...]
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def unique_columns(self) -> "RelationSchema":
        names = [column.name.casefold() for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("relation schema columns must be unique")
        return self


class ColumnProfile(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    data_type: str = Field(min_length=1, max_length=256)
    row_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    minimum: Any | None = None
    maximum: Any | None = None
    mean: float | None = None

    @model_validator(mode="after")
    def valid_counts(self) -> "ColumnProfile":
        if self.null_count > self.row_count:
            raise ValueError("null_count cannot exceed row_count")
        if self.distinct_count > self.row_count - self.null_count:
            raise ValueError("distinct_count cannot exceed non-null rows")
        return self


class DataProfile(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)
    row_count: int = Field(ge=0)
    columns: tuple[ColumnProfile, ...]
    generated_at: datetime = Field(default_factory=utc_now)


class ProfileMismatch(ContractModel):
    column: str | None = Field(default=None, max_length=256)
    metric: Literal[
        "column_present",
        "row_count",
        "null_count",
        "distinct_count",
        "minimum",
        "maximum",
        "mean",
    ]
    expected: Any | None = None
    actual: Any | None = None


class ProfileComparison(ContractModel):
    total_checks: int = Field(ge=1)
    matched_checks: int = Field(ge=0)
    agreement_rate: float = Field(ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    passed: bool
    mismatches: tuple[ProfileMismatch, ...]
    compared_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_summary(self) -> "ProfileComparison":
        if self.matched_checks > self.total_checks:
            raise ValueError("matched_checks cannot exceed total_checks")
        expected_rate = self.matched_checks / self.total_checks
        if abs(self.agreement_rate - expected_rate) > 1e-12:
            raise ValueError("agreement_rate must match comparison counts")
        if self.passed != (self.agreement_rate >= self.threshold):
            raise ValueError("profile comparison status must match threshold")
        if len(self.mismatches) != self.total_checks - self.matched_checks:
            raise ValueError("mismatch count must match failed checks")
        return self


class QualityRule(ContractModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    column: str = Field(min_length=1, max_length=256)
    rule_type: Literal["not_null", "unique", "accepted_values", "range"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["warning", "error"] = "error"

    @model_validator(mode="after")
    def validate_parameters(self) -> "QualityRule":
        if self.rule_type in {"not_null", "unique"} and self.parameters:
            raise ValueError(f"{self.rule_type} does not accept parameters")
        if self.rule_type == "accepted_values":
            values = self.parameters.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError("accepted_values requires a non-empty values list")
        if self.rule_type == "range":
            if not {"minimum", "maximum"} & self.parameters.keys():
                raise ValueError("range requires minimum, maximum, or both")
            unknown = self.parameters.keys() - {"minimum", "maximum"}
            if unknown:
                raise ValueError(f"unsupported range parameters: {sorted(unknown)}")
        return self


class QualityRuleProposal(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)
    rules: tuple[QualityRule, ...] = Field(min_length=1)
    evidence_basis: Literal["schema_profile"] = "schema_profile"
    notes: tuple[str, ...] = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def unique_rules(self) -> "QualityRuleProposal":
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("quality proposal rule ids must be unique")
        return self


class QualityCheckResult(ContractModel):
    rule_id: str = Field(min_length=1, max_length=64)
    column: str = Field(min_length=1, max_length=256)
    rule_type: Literal["not_null", "unique", "accepted_values", "range"]
    severity: Literal["warning", "error"]
    passed: bool
    violation_count: int = Field(ge=0)


class QualityReport(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)
    row_count: int = Field(ge=0)
    checks: tuple[QualityCheckResult, ...]
    passed: bool
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "QualityReport":
        expected = not any(not check.passed and check.severity == "error" for check in self.checks)
        if self.passed != expected:
            raise ValueError("quality report status must match error-severity checks")
        return self


class ColumnChange(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    change_type: Literal["added", "removed", "type_changed", "nullability_changed"]
    before: ColumnSchema | None = None
    after: ColumnSchema | None = None


class SchemaDiff(ContractModel):
    relation: str = Field(min_length=1, max_length=512)
    changes: tuple[ColumnChange, ...]
    breaking: bool

    @model_validator(mode="after")
    def breaking_matches_changes(self) -> "SchemaDiff":
        expected = any(
            item.change_type in {"removed", "type_changed", "nullability_changed"}
            for item in self.changes
        )
        if self.breaking != expected:
            raise ValueError("breaking must match the contained schema changes")
        return self


class TransformationTest(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    assertion: Literal["not_null", "unique", "accepted_values", "row_count_nonzero"]
    column: str | None = Field(default=None, min_length=1, max_length=256)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def column_matches_assertion(self) -> "TransformationTest":
        if self.assertion == "row_count_nonzero" and self.column is not None:
            raise ValueError("row_count_nonzero does not accept a column")
        if self.assertion != "row_count_nonzero" and self.column is None:
            raise ValueError(f"{self.assertion} requires a column")
        if self.assertion in {"not_null", "unique", "row_count_nonzero"} and self.parameters:
            raise ValueError(f"{self.assertion} does not accept parameters")
        if self.assertion == "accepted_values":
            values = self.parameters.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError("accepted_values requires a non-empty values list")
        return self


class TransformationProposal(ContractModel):
    proposal_id: UUID = Field(default_factory=uuid4)
    connector_id: str = Field(min_length=1, max_length=128)
    target_relation: str = Field(min_length=1, max_length=512)
    dialect: str = Field(min_length=1, max_length=64)
    select_sql: str = Field(min_length=1, max_length=100_000)
    dependencies: tuple[str, ...]
    tests: tuple[TransformationTest, ...]
    impact_summary: str = Field(min_length=1, max_length=4_000)
    rollback_plan: str = Field(min_length=1, max_length=4_000)
    dry_run_only: Literal[True] = True
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def complete_proposal(self) -> "TransformationProposal":
        if not self.dependencies:
            raise ValueError("a transformation proposal requires at least one dependency")
        if len({item.casefold() for item in self.dependencies}) != len(self.dependencies):
            raise ValueError("transformation dependencies must be unique")
        if not self.tests:
            raise ValueError("a transformation proposal requires at least one test")
        names = [test.name for test in self.tests]
        if len(names) != len(set(names)):
            raise ValueError("transformation test names must be unique")
        return self


class LineageAsset(ContractModel):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    asset_type: Literal["source", "table", "model", "metric", "dashboard"]
    depends_on: tuple[str, ...] = ()


class LineageSnapshot(ContractModel):
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    dbt_schema_version: str = Field(min_length=1, max_length=512)
    dbt_version: str = Field(min_length=1, max_length=64)
    assets: tuple[LineageAsset, ...]
    unresolved_dependencies: tuple[str, ...] = ()
    complete: bool
    loaded_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_graph_summary(self) -> "LineageSnapshot":
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("lineage snapshot asset ids must be unique")
        if len(self.unresolved_dependencies) != len(set(self.unresolved_dependencies)):
            raise ValueError("unresolved lineage dependencies must be unique")
        if self.complete != (not self.unresolved_dependencies):
            raise ValueError("lineage snapshot completeness must match unresolved dependencies")
        return self


class ImpactedAsset(ContractModel):
    asset_id: str = Field(min_length=1, max_length=256)
    asset_type: Literal["source", "table", "model", "metric", "dashboard"]
    distance: int = Field(ge=1)
    path: tuple[str, ...] = Field(min_length=2)


class ImpactAnalysis(ContractModel):
    changed_assets: tuple[str, ...] = Field(min_length=1)
    impacted_assets: tuple[ImpactedAsset, ...]
    has_cycles: bool
    generated_at: datetime = Field(default_factory=utc_now)


class ProfileBatchRequest(ContractModel):
    request_id: UUID = Field(default_factory=uuid4)
    task_id: UUID = Field(default_factory=uuid4)
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)


class ProfileBatchItem(ContractModel):
    request_id: UUID
    task_id: UUID
    connector_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(min_length=1, max_length=512)
    status: Literal["succeeded", "failed", "cancelled"]
    elapsed_ms: float = Field(ge=0)
    profile: DataProfile | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def result_matches_status(self) -> "ProfileBatchItem":
        if self.status == "succeeded":
            if (
                self.profile is None
                or self.error_code is not None
                or self.error_message is not None
            ):
                raise ValueError("a successful profile item requires only a profile")
        elif self.profile is not None or not self.error_code or not self.error_message:
            raise ValueError("a failed profile item requires error_code and error_message")
        return self


class ProfileBatchResult(ContractModel):
    items: tuple[ProfileBatchItem, ...]
    all_succeeded: bool
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def summary_matches_items(self) -> "ProfileBatchResult":
        if len({item.request_id for item in self.items}) != len(self.items):
            raise ValueError("profile batch request ids must be unique")
        expected = all(item.status == "succeeded" for item in self.items)
        if self.all_succeeded != expected:
            raise ValueError("all_succeeded must match batch items")
        return self


class ProfileLoadGateResult(ContractModel):
    request_count: int = Field(ge=1)
    max_workers: int = Field(ge=1, le=32)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    p95_request_ms: float = Field(ge=0)
    max_p95_ms: float = Field(gt=0)
    passed: bool
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def summary_matches_gate(self) -> "ProfileLoadGateResult":
        if self.succeeded + self.failed + self.cancelled != self.request_count:
            raise ValueError("load gate outcomes must match request_count")
        expected = (
            self.failed == 0
            and self.cancelled == 0
            and self.succeeded == self.request_count
            and self.artifact_count == self.succeeded
            and self.p95_request_ms <= self.max_p95_ms
        )
        if self.passed != expected:
            raise ValueError("load gate status must match outcomes, artifacts, and latency")
        return self


class DbtCompileReport(ContractModel):
    project_name: str = Field(min_length=1, max_length=256)
    target: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    selected_resources: tuple[str, ...] = ()
    command: tuple[str, ...] = Field(min_length=1)
    exit_code: int
    succeeded: bool
    elapsed_ms: float = Field(ge=0)
    stdout_tail: str = Field(max_length=20_000)
    stderr_tail: str = Field(max_length=20_000)
    production_write_enabled: Literal[False] = False

    @model_validator(mode="after")
    def status_matches_exit_code(self) -> "DbtCompileReport":
        if self.succeeded != (self.exit_code == 0):
            raise ValueError("dbt compile status must match exit_code")
        return self


class DbtTestReport(ContractModel):
    project_name: str = Field(min_length=1, max_length=256)
    target: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    selected_resources: tuple[str, ...] = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    exit_code: int
    succeeded: bool
    elapsed_ms: float = Field(ge=0)
    stdout_tail: str = Field(max_length=20_000)
    stderr_tail: str = Field(max_length=20_000)
    production_write_enabled: Literal[False] = False

    @model_validator(mode="after")
    def status_matches_exit_code(self) -> "DbtTestReport":
        if self.succeeded != (self.exit_code == 0):
            raise ValueError("dbt test status must match exit_code")
        return self


class DevelopmentTestResult(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    assertion: Literal["not_null", "unique", "accepted_values", "row_count_nonzero"]
    passed: Literal[True] = True
    violation_count: int = Field(ge=0)


class DevelopmentWriteReport(ContractModel):
    proposal_id: UUID
    connector_id: str = Field(min_length=1, max_length=128)
    target_relation: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    environment: Literal["development", "ci"]
    rows_written: int = Field(ge=0)
    tests: tuple[DevelopmentTestResult, ...] = Field(min_length=1)
    rollback_sql: str = Field(min_length=1, max_length=1_000)
    succeeded: Literal[True] = True
    production_write_enabled: Literal[False] = False
    executed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def successful_tests_only(self) -> "DevelopmentWriteReport":
        if any(result.violation_count != 0 for result in self.tests):
            raise ValueError("development write report can contain only passing tests")
        return self
