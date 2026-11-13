"""Reviewed physical cleanup of expired private XLSX sources.

A cleanup consumes one exact, one-time approval for a previously observed maintenance plan, removes
only the selected expired ``xlsx_uploads`` rows, and writes a content-free tombstone in the same
transaction. Derived snapshots live inside the removed row and disappear with it; no separate
artifact, report or index exists to preserve. Rows may remain in database free pages, WAL files or
backups, so the receipt never claims safe erasure.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_preflight import MAX_BYTES
from generaldataagent.memory.display_preferences import canonical_hash

MAX_SELECTION = 200


class XlsxSourceTombstone(ContractModel):
    """Durable, content-free record of one physically removed private XLSX row."""

    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    owner_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["staged", "confirmed"]
    snapshot_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_bytes: int = Field(ge=1, le=MAX_BYTES, strict=True)
    removed_at: AwareDatetime
    approval_id: UUID
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tombstone_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent_tombstone(self):
        if self.status != "confirmed" and self.snapshot_hash is not None:
            raise ValueError("xlsx_cleanup_tombstone_invalid")
        payload = tombstone_payload(
            file_id=self.file_id,
            task_id=self.task_id,
            owner_ref=self.owner_ref,
            status=self.status,
            snapshot_hash=self.snapshot_hash,
            source_hash=self.source_hash,
            source_bytes=self.source_bytes,
            approval_id=self.approval_id,
            inventory_digest=self.inventory_digest,
        )
        if self.tombstone_digest != tombstone_digest(payload):
            raise ValueError("xlsx_cleanup_tombstone_digest_mismatch")
        return self


def tombstone_payload(
    *,
    file_id: UUID,
    task_id: UUID,
    owner_ref: str,
    status: str,
    snapshot_hash: str | None,
    source_hash: str,
    source_bytes: int,
    approval_id: UUID,
    inventory_digest: str,
) -> dict:
    """Bind the content-free facts an approver reviewed; the removal time stays observational."""

    return {
        "schema_version": "1.0",
        "file_id": str(file_id),
        "task_id": str(task_id),
        "owner_ref": owner_ref,
        "status": status,
        "snapshot_hash": snapshot_hash,
        "source_hash": source_hash,
        "source_bytes": source_bytes,
        "approval_id": str(approval_id),
        "inventory_digest": inventory_digest,
    }


def tombstone_digest(payload: dict) -> str:
    return canonical_hash(payload)


class XlsxCleanupItem(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    owner_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["staged", "confirmed"]
    snapshot_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_bytes: int = Field(ge=1, le=MAX_BYTES, strict=True)
    outcome: Literal["removed", "already_removed"]

    @model_validator(mode="after")
    def item_is_coherent(self):
        if self.status != "confirmed" and self.snapshot_hash is not None:
            raise ValueError("xlsx_cleanup_item_invalid")
        return self


class XlsxCleanupReceipt(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: UUID
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    epoch: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_files: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_SELECTION)
    items: tuple[XlsxCleanupItem, ...] = Field(min_length=1, max_length=MAX_SELECTION)
    removed_files: int = Field(ge=0, le=MAX_SELECTION, strict=True)
    already_removed_files: int = Field(ge=0, le=MAX_SELECTION, strict=True)
    reclaimed_source_bytes: int = Field(ge=0, le=MAX_SELECTION * MAX_BYTES, strict=True)
    remaining_files: int = Field(ge=0, le=MAX_SELECTION, strict=True)
    remaining_source_bytes: int = Field(ge=0, le=MAX_SELECTION * MAX_BYTES, strict=True)
    started_at: AwareDatetime
    completed_at: AwareDatetime
    evidence_artifacts_removed: Literal[False] = False
    safe_erasure: Literal[False] = False
    destructive_actions_performed: Literal[True] = True
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent_receipt(self):
        requested = tuple(str(value) for value in self.requested_files)
        item_ids = tuple(str(item.file_id) for item in self.items)
        if (
            requested != tuple(sorted(requested))
            or len(set(requested)) != len(requested)
            or item_ids != requested
        ):
            raise ValueError("xlsx_cleanup_receipt_selection_invalid")
        removed = sum(item.outcome == "removed" for item in self.items)
        if (
            self.removed_files != removed
            or self.already_removed_files != len(self.items) - removed
            or self.reclaimed_source_bytes
            != sum(item.source_bytes for item in self.items if item.outcome == "removed")
            or self.completed_at < self.started_at
        ):
            raise ValueError("xlsx_cleanup_receipt_totals_invalid")
        payload = cleanup_receipt_payload(
            approval_id=self.approval_id,
            inventory_digest=self.inventory_digest,
            epoch=self.epoch,
            requested_files=self.requested_files,
            items=self.items,
        )
        if self.receipt_digest != cleanup_receipt_digest(payload):
            raise ValueError("xlsx_cleanup_receipt_digest_mismatch")
        return self


def cleanup_receipt_payload(
    *,
    approval_id: UUID,
    inventory_digest: str,
    epoch: str,
    requested_files: tuple[UUID, ...],
    items: tuple[XlsxCleanupItem, ...],
) -> dict:
    """Bind approval, selection and source identity; outcomes and times stay observational."""

    return {
        "schema_version": "1.0",
        "approval_id": str(approval_id),
        "inventory_digest": inventory_digest,
        "epoch": epoch,
        "requested_files": [str(value) for value in requested_files],
        "sources": [
            {
                "file_id": str(item.file_id),
                "task_id": str(item.task_id),
                "owner_ref": item.owner_ref,
                "status": item.status,
                "snapshot_hash": item.snapshot_hash,
                "source_hash": item.source_hash,
                "source_bytes": item.source_bytes,
            }
            for item in items
        ],
    }


def cleanup_receipt_digest(payload: dict) -> str:
    """Hash a payload produced by :func:`cleanup_receipt_payload`."""

    return canonical_hash(payload)
