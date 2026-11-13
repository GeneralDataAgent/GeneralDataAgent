"""Read-only retained-XLSX inventory and reviewable capacity plan contracts.

The plan never authorizes or performs deletion. It lists retained and expired private workbooks with
stable metadata references, reports per-creator retained capacity and marks snapshots that no longer
match the current declaration. It never contains cell values and binds the observation to one digest
so a later reviewed maintenance step can detect drift.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_intake_contracts import MAX_SNAPSHOT_CELLS
from generaldataagent.datasets.xlsx_preflight import MAX_BYTES, MAX_ROWS
from generaldataagent.memory.display_preferences import canonical_hash

ReviewState = Literal["retain", "expired_review_required"]
SnapshotState = Literal["none", "current", "stale"]


class XlsxMaintenanceFile(ContractModel):
    """Metadata for one retained workbook; no header text, cell value, formula or key material."""

    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    owner_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["staged", "confirmed"]
    availability: Literal["compatible", "release_incompatible"]
    has_interpretation: bool
    snapshot_state: SnapshotState
    snapshot_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_rows: int | None = Field(default=None, ge=0, le=MAX_ROWS, strict=True)
    snapshot_cells: int | None = Field(default=None, ge=0, le=MAX_SNAPSHOT_CELLS, strict=True)
    snapshot_duplicate_rows: int | None = Field(default=None, ge=0, le=MAX_ROWS, strict=True)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_bytes: int = Field(ge=1, le=MAX_BYTES, strict=True)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expired: bool
    review_state: ReviewState

    @model_validator(mode="after")
    def entry_is_coherent(self) -> "XlsxMaintenanceFile":
        snapshot = (
            self.snapshot_hash,
            self.snapshot_rows,
            self.snapshot_cells,
            self.snapshot_duplicate_rows,
        )
        if self.snapshot_state == "none":
            if any(value is not None for value in snapshot):
                raise ValueError("xlsx_maintenance_snapshot_state_invalid")
        elif any(value is None for value in snapshot) or self.status != "confirmed":
            raise ValueError("xlsx_maintenance_snapshot_state_invalid")
        if self.has_interpretation and self.status != "confirmed":
            raise ValueError("xlsx_maintenance_interpretation_state_invalid")
        if self.expired != (self.review_state == "expired_review_required"):
            raise ValueError("xlsx_maintenance_review_state_invalid")
        if self.expires_at <= self.created_at:
            raise ValueError("xlsx_maintenance_lifetime_invalid")
        return self


class XlsxMaintenanceScope(ContractModel):
    """Retained capacity for one keyed creator scope, represented only by a truncated HMAC."""

    schema_version: Literal["1.0"] = "1.0"
    owner_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    retained_files: int = Field(ge=1, le=20, strict=True)
    expired_files: int = Field(ge=0, le=20, strict=True)
    source_bytes: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def expired_within_retained(self) -> "XlsxMaintenanceScope":
        if self.expired_files > self.retained_files:
            raise ValueError("xlsx_maintenance_scope_invalid")
        return self


class XlsxMaintenanceSummary(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    retained_files: int = Field(ge=0, le=200, strict=True)
    expired_files: int = Field(ge=0, le=200, strict=True)
    confirmed_files: int = Field(ge=0, le=200, strict=True)
    staged_files: int = Field(ge=0, le=200, strict=True)
    interpreted_files: int = Field(ge=0, le=200, strict=True)
    snapshot_files: int = Field(ge=0, le=200, strict=True)
    stale_snapshots: int = Field(ge=0, le=200, strict=True)
    retained_source_bytes: int = Field(ge=0, strict=True)
    expired_source_bytes: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_are_complete(self) -> "XlsxMaintenanceSummary":
        if self.confirmed_files + self.staged_files != self.retained_files:
            raise ValueError("xlsx_maintenance_summary_invalid")
        if (
            self.expired_files > self.retained_files
            or self.interpreted_files > self.confirmed_files
            or self.snapshot_files > self.interpreted_files
            or self.stale_snapshots > self.snapshot_files
            or self.expired_source_bytes > self.retained_source_bytes
        ):
            raise ValueError("xlsx_maintenance_summary_invalid")
        return self


class XlsxMaintenanceCapacity(ContractModel):
    """Configured retention bounds and the headroom a later cleanup would release."""

    schema_version: Literal["1.0"] = "1.0"
    max_files_per_actor: int = Field(ge=1, le=20, strict=True)
    max_total_files: int = Field(ge=1, le=200, strict=True)
    retained_files: int = Field(ge=0, le=200, strict=True)
    headroom_files: int = Field(ge=0, le=200, strict=True)

    @model_validator(mode="after")
    def headroom_matches(self) -> "XlsxMaintenanceCapacity":
        if self.retained_files > self.max_total_files:
            raise ValueError("xlsx_maintenance_capacity_invalid")
        if self.headroom_files != self.max_total_files - self.retained_files:
            raise ValueError("xlsx_maintenance_capacity_invalid")
        return self


class XlsxMaintenancePlan(ContractModel):
    """One observed inventory. It is an observation, not a cleanup permission or a deletion."""

    schema_version: Literal["1.0"] = "1.0"
    observed_at: AwareDatetime
    epoch: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["complete"] = "complete"
    capacity: XlsxMaintenanceCapacity
    summary: XlsxMaintenanceSummary
    scopes: tuple[XlsxMaintenanceScope, ...] = Field(max_length=200)
    files: tuple[XlsxMaintenanceFile, ...] = Field(max_length=200)
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    values: Literal["not-read-by-this-inventory"] = "not-read-by-this-inventory"
    deletes: Literal[False] = False
    releases_capacity: Literal[False] = False
    audit: Literal["not-emitted"] = "not-emitted"

    @model_validator(mode="after")
    def plan_is_complete(self) -> "XlsxMaintenancePlan":
        if len(self.files) != self.summary.retained_files:
            raise ValueError("xlsx_maintenance_summary_mismatch")
        if len(self.scopes) != len({scope.owner_ref for scope in self.scopes}):
            raise ValueError("xlsx_maintenance_scope_duplicate")
        if [scope.owner_ref for scope in self.scopes] != sorted(
            scope.owner_ref for scope in self.scopes
        ):
            raise ValueError("xlsx_maintenance_scope_order_invalid")
        if any(scope.retained_files > self.capacity.max_files_per_actor for scope in self.scopes):
            raise ValueError("xlsx_maintenance_capacity_invalid")
        if sum(scope.retained_files for scope in self.scopes) != len(self.files):
            raise ValueError("xlsx_maintenance_summary_mismatch")
        if sum(scope.expired_files for scope in self.scopes) != self.summary.expired_files:
            raise ValueError("xlsx_maintenance_summary_mismatch")
        if sum(scope.source_bytes for scope in self.scopes) != self.summary.retained_source_bytes:
            raise ValueError("xlsx_maintenance_summary_mismatch")
        if self.inventory_digest != maintenance_digest(self.epoch, self.files):
            raise ValueError("xlsx_maintenance_digest_mismatch")
        return self


def maintenance_digest(epoch: str, files: tuple[XlsxMaintenanceFile, ...]) -> str:
    return canonical_hash(
        {
            "epoch": epoch,
            "files": [entry.model_dump(mode="json") for entry in files],
        }
    )
