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
