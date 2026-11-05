"""Read-only retained-CSV inventory and reviewable maintenance plan contracts.

The plan never authorizes or performs deletion. It lists retained and expired files with stable
source/report references, flags every file whose references or legal-hold state cannot be verified,
and binds the observation to one digest so a later reviewed maintenance step can detect drift.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

BlockedReason = Literal[
    "legal_hold",
    "evidence_missing",
    "evidence_deleted",
    "evidence_expired",
    "evidence_scope_mismatch",
    "report_unreadable",
]


class CsvMaintenanceFile(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    owner_ref: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: Literal["staged", "profiled"]
    availability: Literal["compatible", "release_incompatible"]
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_bytes: int = Field(ge=0, le=1048576, strict=True)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expired: bool = Field(strict=True)
    report_state: Literal["absent", "readable", "unreadable"]
    report_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_artifact_ids: tuple[UUID, ...] = Field(max_length=2)
    blocked_reasons: tuple[BlockedReason, ...] = Field(max_length=6)
    review_state: Literal["retain", "expired_review_required", "blocked"]

    @model_validator(mode="after")
    def consistent_inventory_entry(self):
        evidence = tuple(str(value) for value in self.evidence_artifact_ids)
        if self.expires_at <= self.created_at:
            raise ValueError("csv_maintenance_lifetime_invalid")
        if (
            len(set(evidence)) != len(evidence)
            or evidence != tuple(sorted(evidence))
            or len(set(self.blocked_reasons)) != len(self.blocked_reasons)
            or (self.review_state == "blocked") != bool(self.blocked_reasons)
        ):
            raise ValueError("csv_maintenance_entry_invalid")
        if self.review_state == "retain" and self.blocked_reasons:
            raise ValueError("csv_maintenance_retained_file_must_not_be_blocked")
        if self.review_state == "expired_review_required" and (
            not self.expired or self.blocked_reasons
        ):
            raise ValueError("csv_maintenance_expired_file_must_be_reviewed")
        if (
            self.review_state == "blocked"
            and not self.expired
            and self.blocked_reasons != ("report_unreadable",)
        ):
            raise ValueError("csv_maintenance_unexpired_file_must_be_retained")
        if self.report_state == "absent":
            if self.status != "staged" or self.report_digest is not None or evidence:
                raise ValueError("csv_maintenance_absent_report_invalid")
        elif self.report_state == "readable":
            if self.status != "profiled" or self.report_digest is None or len(evidence) != 2:
                raise ValueError("csv_maintenance_readable_report_invalid")
        elif (
            self.status != "profiled"
            or self.report_digest is not None
            or evidence
            or "report_unreadable" not in self.blocked_reasons
        ):
            raise ValueError("csv_maintenance_unreadable_report_invalid")
        return self


class CsvMaintenanceSummary(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    retained_files: int = Field(ge=0, le=200, strict=True)
    expired_files: int = Field(ge=0, le=200, strict=True)
    review_required_files: int = Field(ge=0, le=200, strict=True)
    blocked_files: int = Field(ge=0, le=200, strict=True)
    staged_files: int = Field(ge=0, le=200, strict=True)
    profiled_files: int = Field(ge=0, le=200, strict=True)
    retained_source_bytes: int = Field(ge=0, le=200 * 1048576, strict=True)
    expired_source_bytes: int = Field(ge=0, le=200 * 1048576, strict=True)


class CsvMaintenancePlan(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    observed_at: AwareDatetime
    epoch: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["complete", "blocked"]
    summary: CsvMaintenanceSummary
    files: tuple[CsvMaintenanceFile, ...] = Field(max_length=200)
    destructive_actions_performed: Literal[False] = False

    @model_validator(mode="after")
    def consistent_plan(self):
        ordered = tuple(sorted(self.files, key=lambda item: (item.created_at, str(item.file_id))))
        if ordered != self.files or len({item.file_id for item in self.files}) != len(self.files):
            raise ValueError("csv_maintenance_order_or_identity_invalid")
        if any(item.expired != (item.expires_at <= self.observed_at) for item in self.files):
            raise ValueError("csv_maintenance_expiry_observation_invalid")
        expected = {
            "retained_files": len(self.files),
            "expired_files": sum(item.expired for item in self.files),
            "review_required_files": sum(
                item.review_state == "expired_review_required" for item in self.files
            ),
            "blocked_files": sum(item.review_state == "blocked" for item in self.files),
            "staged_files": sum(item.status == "staged" for item in self.files),
            "profiled_files": sum(item.status == "profiled" for item in self.files),
            "retained_source_bytes": sum(item.source_bytes for item in self.files),
            "expired_source_bytes": sum(item.source_bytes for item in self.files if item.expired),
        }
        if any(getattr(self.summary, field) != value for field, value in expected.items()):
            raise ValueError("csv_maintenance_summary_mismatch")
        if self.status != ("blocked" if self.summary.blocked_files else "complete"):
            raise ValueError("csv_maintenance_status_mismatch")
        if self.inventory_digest != maintenance_digest(self.epoch, self.files):
            raise ValueError("csv_maintenance_digest_mismatch")
        return self


def maintenance_digest(epoch: str, files: tuple[CsvMaintenanceFile, ...]) -> str:
    """Digest immutable retained facts only; expiry and hold state are observations."""

    return canonical_hash(
        {
            "epoch": epoch,
            "files": [
                {
                    "file_id": str(item.file_id),
                    "task_id": str(item.task_id),
                    "owner_ref": item.owner_ref,
                    "status": item.status,
                    "availability": item.availability,
                    "source_hash": item.source_hash,
                    "source_bytes": item.source_bytes,
                    "created_at": item.created_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                    "report_state": item.report_state,
                    "report_digest": item.report_digest,
                    "evidence_artifact_ids": [str(value) for value in item.evidence_artifact_ids],
                }
                for item in files
            ],
        }
    )
