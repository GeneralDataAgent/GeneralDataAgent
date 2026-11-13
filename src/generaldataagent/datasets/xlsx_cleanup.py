"""Consume one exact approval to physically remove reviewed expired private XLSX sources.

The operator reviews an ``XlsxMaintenancePlan``, requests approval for its exact
``inventory_digest`` and an explicit sorted file selection, then runs this port with the approval
database and approval ID. The port re-observes the plan, refuses any drift or blocked entry,
consumes the one-time grant, and removes only the selected ``xlsx_uploads`` rows while writing a
content-free tombstone in the same transaction. No artifact index, report or evidence store is
touched; free pages, WAL files and backups may still contain bytes, so no safe-erasure claim is
made.
"""

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from cryptography.exceptions import InvalidTag

from generaldataagent.datasets.xlsx_cleanup_contracts import (
    MAX_SELECTION,
    XlsxCleanupItem,
    XlsxCleanupReceipt,
    XlsxSourceTombstone,
    cleanup_receipt_digest,
    cleanup_receipt_payload,
    tombstone_digest,
    tombstone_payload,
)
from generaldataagent.datasets.xlsx_intake_contracts import XlsxLimits
from generaldataagent.datasets.xlsx_maintenance import (
    XlsxMaintenanceInventory,
    XlsxMaintenancePlan,
    XlsxMaintenanceUnavailable,
    _columns,
    load_key_file,
)
from generaldataagent.policy.approvals import ApprovalError, SQLiteApprovalStore

_TOMBSTONE_TABLE = "xlsx_source_tombstones"
_TOMBSTONE_COLUMNS = ["file_id", "body"]
_TOOL_NAME = "xlsx.maintenance.remove"
_ACTION = "remove-expired-source"
_DIGEST_PATTERN = "sha256:"


class XlsxCleanupRefused(PermissionError):
    """The approval or selection does not authorize this cleanup."""


class XlsxCleanupUnavailable(RuntimeError):
    """The configured state cannot be written safely; nothing was changed."""


class XlsxCleanupConflict(RuntimeError):
    """Observed rows or tombstones contradict the approved selection."""


def _write_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=rw"


@contextmanager
def _write_snapshot(path: Path, code: str):
    if path.is_symlink() or not path.is_file():
        raise XlsxCleanupUnavailable(code)
    try:
        db = sqlite3.connect(_write_uri(path), uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise XlsxCleanupUnavailable(code) from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        raise XlsxCleanupUnavailable(code) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class XlsxSourceCleanup:
    def __init__(
        self,
        *,
        store_path: Path,
        encryption_key: bytes,
        approval_db: Path,
        clock: Callable[[], datetime] | None = None,
        limits: XlsxLimits | None = None,
        max_files: int = 200,
        max_files_per_actor: int = 20,
    ):
        self.store_path = Path(store_path)
        self.approval_db = Path(approval_db)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.inventory = XlsxMaintenanceInventory(
            store_path=store_path,
            encryption_key=encryption_key,
            clock=self.clock,
            limits=limits,
            max_files=max_files,
            max_files_per_actor=max_files_per_actor,
        )
        self.view = self.inventory.view
        self.approval_db.parent.mkdir(parents=True, exist_ok=True)

    def now(self) -> datetime:
        return self.inventory.now()

    def _approval(self, approval_id: UUID):
        store = SQLiteApprovalStore(self.approval_db, clock=self.clock)
        try:
            record = store.inspect(approval_id)
        except ApprovalError as exc:
            raise XlsxCleanupRefused("xlsx_cleanup_approval_unknown") from exc
        request = record.request
        if (
            request.tool_name != _TOOL_NAME
            or request.action != _ACTION
            or record.grant is None
            or record.disposition is not None
        ):
            raise XlsxCleanupRefused("xlsx_cleanup_approval_not_granted")
        arguments = dict(request.parameters)
        if set(arguments) != {"inventory_digest", "file_ids"}:
            raise XlsxCleanupRefused("xlsx_cleanup_selection_invalid")
        digest = arguments["inventory_digest"]
        file_ids = arguments["file_ids"]
        if (
            not isinstance(digest, str)
            or not digest.startswith(_DIGEST_PATTERN)
            or len(digest) != len(_DIGEST_PATTERN) + 64
            or request.resource != digest
            or not isinstance(file_ids, list)
            or not 1 <= len(file_ids) <= MAX_SELECTION
            or not all(isinstance(value, str) for value in file_ids)
        ):
            raise XlsxCleanupRefused("xlsx_cleanup_selection_invalid")
        try:
            selection = tuple(sorted({UUID(value) for value in file_ids}, key=str))
        except ValueError as exc:
            raise XlsxCleanupRefused("xlsx_cleanup_selection_invalid") from exc
        if len(selection) != len(file_ids):
            raise XlsxCleanupRefused("xlsx_cleanup_selection_invalid")
        return digest, selection

    def _selected(self, plan: XlsxMaintenancePlan, selection: tuple[UUID, ...]) -> tuple[Any, ...]:
        if plan.status != "complete":
            raise XlsxCleanupRefused("xlsx_cleanup_plan_blocked")
        by_id = {item.file_id: item for item in plan.files}
        entries = []
        for file_id in selection:
            entry = by_id.get(file_id)
            if (
                entry is None
                or entry.review_state != "expired_review_required"
                or not entry.expired
            ):
                raise XlsxCleanupRefused("xlsx_cleanup_selection_not_eligible")
            entries.append(entry)
        return tuple(entries)

    def _verify_schema(self, db: sqlite3.Connection) -> None:
        code = "xlsx_cleanup_store_unavailable"
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise XlsxCleanupUnavailable(code)
        if _columns(db, "xlsx_key") != ["singleton", "body"] or _columns(db, "xlsx_uploads") != [
            "file_id",
            "scope",
            "idem",
            "request_hash",
            "body",
            "source",
        ]:
            raise XlsxCleanupUnavailable(code)
        row = db.execute("SELECT body FROM xlsx_key WHERE singleton=1").fetchone()
        if row is None:
            raise XlsxCleanupUnavailable(code)
        try:
            canary = self.view.open(row["body"], b"xlsx-key-v1")
        except InvalidTag as exc:
            raise XlsxCleanupRefused("xlsx_cleanup_key_mismatch") from exc
        except Exception as exc:
            raise XlsxCleanupUnavailable(code) from exc
        if canary != b"xlsx-intake-v1":
            raise XlsxCleanupRefused("xlsx_cleanup_key_mismatch")
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TOMBSTONE_TABLE,)
            ).fetchone()
            and _columns(db, _TOMBSTONE_TABLE) != _TOMBSTONE_COLUMNS
        ):
            raise XlsxCleanupUnavailable(code)
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {_TOMBSTONE_TABLE} "
            "(file_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )

    def _tombstone(self, db: sqlite3.Connection, file_id: UUID) -> XlsxSourceTombstone | None:
        row = db.execute(
            f"SELECT body FROM {_TOMBSTONE_TABLE} WHERE file_id=?", (str(file_id),)
        ).fetchone()
        if row is None:
            return None
        try:
            return XlsxSourceTombstone.model_validate_json(row["body"])
        except Exception as exc:
            # A tombstone that cannot be authenticated contradicts the approved selection.
            raise XlsxCleanupConflict("xlsx_cleanup_tombstone_conflict") from exc

    @staticmethod
    def _matches(tombstone: XlsxSourceTombstone, entry, approval_id: UUID, digest: str) -> bool:
        return (
            tombstone.approval_id == approval_id
            and tombstone.file_id == entry.file_id
            and tombstone.task_id == entry.task_id
            and tombstone.owner_ref == entry.owner_ref
            and tombstone.status == entry.status
            and tombstone.snapshot_hash == entry.snapshot_hash
            and tombstone.source_hash == entry.source_hash
            and tombstone.source_bytes == entry.source_bytes
            and tombstone.inventory_digest == digest
        )

    def _verify_row(self, row: sqlite3.Row, entry) -> None:
        try:
            payload, raw = self.view.decode(row)
        except Exception as exc:
            raise XlsxCleanupUnavailable("xlsx_cleanup_source_unreadable") from exc
        record = payload.file
        snapshot_hash = payload.snapshot.snapshot_hash if payload.snapshot else None
        if (
            record.file_id != entry.file_id
            or record.task_id != entry.task_id
            or record.status != entry.status
            or record.source_hash != entry.source_hash
            or snapshot_hash != entry.snapshot_hash
            or len(raw) != entry.source_bytes
        ):
            raise XlsxCleanupConflict("xlsx_cleanup_source_changed")

    def _remove(
        self, db: sqlite3.Connection, entry, approval_id: UUID, digest: str
    ) -> XlsxCleanupItem:
        tombstone = self._tombstone(db, entry.file_id)
        row = db.execute(
            "SELECT * FROM xlsx_uploads WHERE file_id=?", (str(entry.file_id),)
        ).fetchone()
        if tombstone is not None:
            if not self._matches(tombstone, entry, approval_id, digest) or row is not None:
                raise XlsxCleanupConflict("xlsx_cleanup_tombstone_conflict")
            outcome = "already_removed"
        else:
            if row is None:
                raise XlsxCleanupConflict("xlsx_cleanup_source_missing")
            self._verify_row(row, entry)
            payload = tombstone_payload(
                file_id=entry.file_id,
                task_id=entry.task_id,
                owner_ref=entry.owner_ref,
                status=entry.status,
                snapshot_hash=entry.snapshot_hash,
                source_hash=entry.source_hash,
                source_bytes=entry.source_bytes,
                approval_id=approval_id,
                inventory_digest=digest,
            )
            tombstone = XlsxSourceTombstone(
                **payload,
                removed_at=self.now(),
                tombstone_digest=tombstone_digest(payload),
            )
            db.execute(
                f"INSERT INTO {_TOMBSTONE_TABLE} (file_id,body) VALUES (?,?)",
                (str(entry.file_id), tombstone.model_dump_json()),
            )
            db.execute("DELETE FROM xlsx_uploads WHERE file_id=?", (str(entry.file_id),))
            outcome = "removed"
        return XlsxCleanupItem(
            file_id=entry.file_id,
            task_id=entry.task_id,
            owner_ref=entry.owner_ref,
            status=entry.status,
            snapshot_hash=entry.snapshot_hash,
            source_hash=entry.source_hash,
            source_bytes=entry.source_bytes,
            outcome=outcome,
        )

    def _replay(self, db: sqlite3.Connection, file_id: UUID, approval_id: UUID, digest: str):
        """A completed cleanup may be replayed after the store drifted; nothing is deleted."""

        tombstone = self._tombstone(db, file_id)
        row = db.execute("SELECT 1 FROM xlsx_uploads WHERE file_id=?", (str(file_id),)).fetchone()
        if (
            tombstone is None
            or row is not None
            or tombstone.file_id != file_id
            or tombstone.approval_id != approval_id
            or tombstone.inventory_digest != digest
        ):
            raise XlsxCleanupRefused("xlsx_cleanup_plan_changed")
        return XlsxCleanupItem(
            file_id=tombstone.file_id,
            task_id=tombstone.task_id,
            owner_ref=tombstone.owner_ref,
            status=tombstone.status,
            snapshot_hash=tombstone.snapshot_hash,
            source_hash=tombstone.source_hash,
            source_bytes=tombstone.source_bytes,
            outcome="already_removed",
        )

    @staticmethod
    def _remaining(db: sqlite3.Connection, view) -> tuple[int, int]:
        remaining = 0
        remaining_bytes = 0
        for row in db.execute("SELECT * FROM xlsx_uploads").fetchall():
            remaining += 1
            try:
                _, raw = view.decode(row)
            except Exception as exc:
                raise XlsxCleanupUnavailable("xlsx_cleanup_store_unreadable") from exc
            remaining_bytes += len(raw)
        return remaining, remaining_bytes

    def execute(self, approval_id: UUID) -> XlsxCleanupReceipt:
        started = self.now()
        digest, selection = self._approval(approval_id)
        try:
            plan = self.inventory.inventory()
        except XlsxMaintenanceUnavailable as exc:
            raise XlsxCleanupUnavailable(str(exc)) from exc

        if plan.inventory_digest == digest:
            entries = self._selected(plan, selection)
            store = SQLiteApprovalStore(self.approval_db, clock=self.clock)
            try:
                store.consume_granted(approval_id)
            except ApprovalError as exc:
                if "already been consumed" not in str(exc):
                    raise XlsxCleanupRefused("xlsx_cleanup_approval_closed") from exc
            with _write_snapshot(self.store_path, "xlsx_cleanup_store_unavailable") as db:
                self._verify_schema(db)
                items = tuple(self._remove(db, entry, approval_id, digest) for entry in entries)
                remaining, remaining_bytes = self._remaining(db, self.view)
        else:
            # Either the plan drifted before anything happened (refuse), or this exact approval
            # already removed every selected row (idempotent replay). Tombstones decide.
            with _write_snapshot(self.store_path, "xlsx_cleanup_store_unavailable") as db:
                self._verify_schema(db)
                items = tuple(
                    self._replay(db, file_id, approval_id, digest) for file_id in selection
                )
                remaining, remaining_bytes = self._remaining(db, self.view)

        payload = cleanup_receipt_payload(
            approval_id=approval_id,
            inventory_digest=digest,
            epoch=plan.epoch,
            requested_files=selection,
            items=items,
        )
        return XlsxCleanupReceipt(
            approval_id=approval_id,
            inventory_digest=digest,
            epoch=plan.epoch,
            requested_files=selection,
            items=items,
            removed_files=sum(item.outcome == "removed" for item in items),
            already_removed_files=sum(item.outcome == "already_removed" for item in items),
            reclaimed_source_bytes=sum(
                item.source_bytes for item in items if item.outcome == "removed"
            ),
            remaining_files=remaining,
            remaining_source_bytes=remaining_bytes,
            started_at=started,
            completed_at=self.now(),
            receipt_digest=cleanup_receipt_digest(payload),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Consume one exact approval and physically remove selected expired XLSX sources."
        )
    )
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--approval-db", required=True, type=Path)
    parser.add_argument("--approval-id", required=True, type=UUID)
    try:
        args = parser.parse_args(argv)
        receipt = XlsxSourceCleanup(
            store_path=args.store,
            encryption_key=load_key_file(args.key_file),
            approval_db=args.approval_db,
        ).execute(args.approval_id)
    except (XlsxCleanupRefused, XlsxCleanupConflict) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, XlsxCleanupRefused) else 5
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except XlsxCleanupUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 4
    print(json.dumps(receipt.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
