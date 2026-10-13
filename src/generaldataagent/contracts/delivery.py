"""Versioned contracts for evidence-grounded analysis delivery."""

from typing import Literal

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ArtifactRef, ContractModel


class TableSpec(ContractModel):
    """A renderable table backed by one immutable data artifact."""

    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[object, ...], ...]
    data_artifact_uri: str = Field(pattern=r"^artifact://")

    @model_validator(mode="after")
    def rows_match_columns(self) -> "TableSpec":
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every table row must match the column count")
        return self


class ChartSpec(ContractModel):
    """A deterministic chart instruction that never embeds a second data copy."""

    chart_type: Literal["kpi", "bar", "line", "table"]
    title: str = Field(min_length=1, max_length=256)
    x_field: str | None = Field(default=None, max_length=128)
    y_field: str = Field(default="metric_value", min_length=1, max_length=128)
    data_artifact_uri: str = Field(pattern=r"^artifact://")

    @model_validator(mode="after")
    def chart_fields_are_consistent(self) -> "ChartSpec":
        if self.chart_type in {"bar", "line"} and self.x_field is None:
            raise ValueError("bar and line charts require an x_field")
        if self.chart_type == "kpi" and self.x_field is not None:
            raise ValueError("kpi charts cannot define an x_field")
        return self


class GroundedClaim(ContractModel):
    """One delivery statement with explicit immutable evidence references."""

    claim_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    claim_type: Literal["fact", "interpretation", "limitation", "recommendation"]
    text: str = Field(min_length=1, max_length=2_000)
    evidence_uris: tuple[str, ...] = Field(min_length=1)


class DeliveryPackage(ContractModel):
    """Validated output consumed by API, Reporter, and downstream renderers."""

    metric: str = Field(min_length=1, max_length=128)
    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[object, ...], ...]
    row_count: int = Field(ge=0)
    table: TableSpec
    chart: ChartSpec
    summary: str = Field(min_length=1, max_length=2_000)
    insights: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    claims: tuple[GroundedClaim, ...] = Field(min_length=1)
    narrative_model: str | None = Field(default=None, max_length=256)
    evidence: tuple[ArtifactRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_present_in_evidence(self) -> "DeliveryPackage":
        if self.columns != self.table.columns or self.rows != self.table.rows:
            raise ValueError("compatibility table fields must match table")
        if self.row_count != len(self.rows):
            raise ValueError("row_count must match rows")
        evidence_uris = {item.uri for item in self.evidence}
        referenced = {
            self.table.data_artifact_uri,
            self.chart.data_artifact_uri,
            *(uri for claim in self.claims for uri in claim.evidence_uris),
        }
        missing = referenced - evidence_uris
        if missing:
            raise ValueError(f"delivery references missing evidence: {sorted(missing)}")
        return self
