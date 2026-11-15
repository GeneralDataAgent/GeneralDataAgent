"""Read-only retained-XLSX inventory for a reviewed capacity plan.

An explicit operator supplies one existing encrypted intake database and its key. This port never
initializes missing state, never parses a workbook, never returns headers or cell values and never
deletes or rewrites anything. It lists retained/expired private workbooks, reports per-creator
capacity and marks snapshots that no longer match the current declaration. Output is an observation,
not a cleanup permission.
"""

import argparse
import base64
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_intake_contracts import XlsxLimits
from generaldataagent.datasets.xlsx_intake_store import XlsxUploadStore
from generaldataagent.datasets.xlsx_maintenance_contracts import (
    XlsxMaintenanceCapacity,
    XlsxMaintenanceFile,
    XlsxMaintenancePlan,
    XlsxMaintenanceScope,
    XlsxMaintenanceSummary,
    maintenance_digest,
)
from generaldataagent.memory.display_preferences import canonical_hash

_UPLOAD_COLUMNS = ["file_id", "scope", "idem", "request_hash", "body", "source"]
_KEY_COLUMNS = ["singleton", "body"]


class XlsxMaintenanceUnavailable(RuntimeError):
    """The configured state cannot be observed completely; no partial plan is emitted."""


class _ReadOnlyXlsxView(XlsxUploadStore):
    """Reuse the released record decoder without initializing or opening the store for writes.

    ``XlsxUploadStore.__init__`` creates schema and parents when missing. This view deliberately
    skips it: the maintenance operator must point at an existing store, and a read-only SQLite
    connection guarantees the inventory cannot create, extend or repair it.
    """

    def __init__(self, encryption_key: bytes, clock: Callable[[], datetime]):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("xlsx_intake_requires_aes256_key")
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-xlsx-intake-scope-v1", "sha256")
        self.clock = clock

    def now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("xlsx_maintenance_clock_must_be_aware")
        return now


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


@contextmanager
def _read_snapshot(path: Path, code: str):
    if path.is_symlink() or not path.is_file():
        raise XlsxMaintenanceUnavailable(code)
    try:
        db = sqlite3.connect(_readonly_uri(path), uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise XlsxMaintenanceUnavailable(code) from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db
    except sqlite3.Error as exc:
        raise XlsxMaintenanceUnavailable(code) from exc
    finally:
        db.close()


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _private_file_bytes(path: Path, code: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise XlsxMaintenanceUnavailable(code)
    metadata = path.stat()
    if hasattr(os, "getuid"):
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise PermissionError("xlsx_maintenance_key_file_not_private")
        if metadata.st_uid != os.getuid():
            raise PermissionError("xlsx_maintenance_key_file_owner_mismatch")
    return path.read_bytes()


def load_key_file(path: Path) -> bytes:
    """Read a 32-byte key from an operator-owned private regular file."""

    raw = _private_file_bytes(path, "xlsx_maintenance_key_file_invalid")
    if not raw or len(raw) > 128:
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid")
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid") from exc
    if len(key) != 32:
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid")
    return key


def load_token_file(path: Path) -> str:
    """Read one opaque session token from an operator-owned private regular file."""

    raw = _private_file_bytes(path, "xlsx_maintenance_key_file_invalid")
    if not raw or len(raw) > 256:
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid")
    try:
        token = raw.strip().decode("ascii")
    except UnicodeDecodeError as exc:
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid") from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_file_invalid")
    return token


class XlsxMaintenanceInventory:
    def __init__(
        self,
        *,
        store_path: Path,
        encryption_key: bytes,
        clock: Callable[[], datetime] | None = None,
        limits: XlsxLimits | None = None,
        max_files: int = 200,
        max_files_per_actor: int = 20,
    ):
        if type(max_files) is not int or not 1 <= max_files <= 200:
            raise ValueError("invalid_xlsx_maintenance_inventory_bound")
        if type(max_files_per_actor) is not int or not 1 <= max_files_per_actor <= 20:
            raise ValueError("invalid_xlsx_maintenance_inventory_bound")
        self.store_path = Path(store_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.view = _ReadOnlyXlsxView(encryption_key, self.clock)
        self.limits = limits or XlsxLimits()
        self.epoch = canonical_hash(
            {
                "processing": xlsx_intake._PROCESSING_FINGERPRINT,
                "limits": self.limits.model_dump(mode="json"),
            }
        )
        self.max_files = max_files
        self.max_files_per_actor = max_files_per_actor

    def now(self) -> datetime:
        return self.view.now()

    def _read_rows(self) -> list[tuple[sqlite3.Row, object]]:
        code = "xlsx_maintenance_store_unavailable"
        with _read_snapshot(self.store_path, code) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise XlsxMaintenanceUnavailable("xlsx_maintenance_schema_unsupported")
            if (
                _columns(db, "xlsx_key") != _KEY_COLUMNS
                or _columns(db, "xlsx_uploads") != _UPLOAD_COLUMNS
            ):
                raise XlsxMaintenanceUnavailable("xlsx_maintenance_schema_unsupported")
            row = db.execute("SELECT body FROM xlsx_key WHERE singleton=1").fetchone()
            if row is None:
                raise XlsxMaintenanceUnavailable("xlsx_maintenance_key_missing")
            try:
                canary = self.view.open(row["body"], b"xlsx-key-v1")
            except InvalidTag as exc:
                raise PermissionError("xlsx_maintenance_key_mismatch") from exc
            except Exception as exc:
                raise XlsxMaintenanceUnavailable(code) from exc
            if canary != b"xlsx-intake-v1":
                raise PermissionError("xlsx_maintenance_key_mismatch")
            rows = db.execute(
                "SELECT * FROM xlsx_uploads ORDER BY rowid LIMIT ?", (self.max_files + 1,)
            ).fetchall()
        if len(rows) > self.max_files:
            raise XlsxMaintenanceUnavailable("xlsx_maintenance_inventory_capacity_invalid")
        return [(row, self._decode(row)) for row in rows]

    def _decode(self, row: sqlite3.Row):
        try:
            return self.view.decode(row)
        except Exception as exc:
            raise XlsxMaintenanceUnavailable("xlsx_maintenance_record_unavailable") from exc

    def _entry(self, payload, raw: bytes, now: datetime) -> XlsxMaintenanceFile:
        record = payload.file
        expired = record.expires_at <= now
        snapshot = payload.snapshot
        if snapshot is None:
            state, snapshot_hash, rows, cells, duplicates = "none", None, None, None, None
        else:
            current = (
                payload.interpretation is not None
                and payload.interpretation.interpretation_hash == snapshot.interpretation_hash
            )
            state = "current" if current else "stale"
            snapshot_hash = snapshot.snapshot_hash
            rows = len(snapshot.rows)
            cells = rows * len(snapshot.columns)
            duplicates = snapshot.duplicate_row_count
        return XlsxMaintenanceFile(
            file_id=record.file_id,
            task_id=record.task_id,
            owner_ref=self.view.scope(payload.actor)[:32],
            status=record.status,
            availability=(
                "compatible"
                if record.parser_fingerprint
                == xlsx_intake.parser_fingerprint(self.limits, record.config)
                and record.processing_fingerprint == xlsx_intake._PROCESSING_FINGERPRINT
                else "release_incompatible"
            ),
            has_interpretation=payload.interpretation is not None,
            snapshot_state=state,
            snapshot_hash=snapshot_hash,
            snapshot_rows=rows,
            snapshot_cells=cells,
            snapshot_duplicate_rows=duplicates,
            source_hash=record.source_hash,
            source_bytes=len(raw),
            created_at=record.created_at,
            expires_at=record.expires_at,
            expired=expired,
            review_state="expired_review_required" if expired else "retain",
        )

    def inventory(self) -> XlsxMaintenancePlan:
        now = self.now()
        decoded = self._read_rows()
        files = tuple(
            sorted(
                (self._entry(payload, raw, now) for _, (payload, raw) in decoded),
                key=lambda item: (item.created_at, str(item.file_id)),
            )
        )
        grouped: dict[str, list[XlsxMaintenanceFile]] = {}
        for entry in files:
            grouped.setdefault(entry.owner_ref, []).append(entry)
        scopes = tuple(
            XlsxMaintenanceScope(
                owner_ref=owner_ref,
                retained_files=len(entries),
                expired_files=sum(entry.expired for entry in entries),
                source_bytes=sum(entry.source_bytes for entry in entries),
            )
            for owner_ref, entries in sorted(grouped.items())
        )
        summary = XlsxMaintenanceSummary(
            retained_files=len(files),
            expired_files=sum(entry.expired for entry in files),
            confirmed_files=sum(entry.status == "confirmed" for entry in files),
            staged_files=sum(entry.status == "staged" for entry in files),
            interpreted_files=sum(entry.has_interpretation for entry in files),
            snapshot_files=sum(entry.snapshot_state != "none" for entry in files),
            stale_snapshots=sum(entry.snapshot_state == "stale" for entry in files),
            retained_source_bytes=sum(entry.source_bytes for entry in files),
            expired_source_bytes=sum(entry.source_bytes for entry in files if entry.expired),
        )
        capacity = XlsxMaintenanceCapacity(
            max_files_per_actor=self.max_files_per_actor,
            max_total_files=self.max_files,
            retained_files=len(files),
            headroom_files=self.max_files - len(files),
        )
        return XlsxMaintenancePlan(
            observed_at=now,
            epoch=self.epoch,
            capacity=capacity,
            summary=summary,
            scopes=scopes,
            files=files,
            inventory_digest=maintenance_digest(self.epoch, files),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only retained XLSX inventory for reviewed capacity (never deletes)."
    )
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--summary", action="store_true")
    try:
        args = parser.parse_args(argv)
        plan = XlsxMaintenanceInventory(
            store_path=args.store,
            encryption_key=load_key_file(args.key_file),
        ).inventory()
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except XlsxMaintenanceUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 4
    output = (
        {
            "status": plan.status,
            "observed_at": plan.observed_at.isoformat(),
            "inventory_digest": plan.inventory_digest,
            "capacity": plan.capacity.model_dump(mode="json"),
            "summary": plan.summary.model_dump(mode="json"),
            "values": plan.values,
            "deletes": plan.deletes,
            "releases_capacity": plan.releases_capacity,
        }
        if args.summary
        else plan.model_dump(mode="json")
    )
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
