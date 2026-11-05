"""Read-only retained-CSV inventory for a reviewed maintenance plan.

An explicit operator supplies one existing encrypted intake database, its key and the existing
governed artifact index. This port never initializes missing state, never parses CSV, never
executes profiling and never deletes or rewrites anything. It lists retained/expired capacity with
stable source and report references, and marks a file blocked whenever evidence references, hold
state or report binding cannot be verified. Output is an observation, not a cleanup permission.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
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

from generaldataagent.contracts import ActorContext
from generaldataagent.datasets import csv_intake, csv_parser
from generaldataagent.datasets.csv_intake_contracts import CsvProfileResult
from generaldataagent.datasets.csv_intake_store import CsvUploadStore
from generaldataagent.datasets.csv_maintenance_contracts import (
    CsvMaintenanceFile,
    CsvMaintenancePlan,
    CsvMaintenanceSummary,
    maintenance_digest,
)
from generaldataagent.memory.display_preferences import canonical_hash

_UPLOAD_COLUMNS = ["file_id", "scope", "idem", "request_hash", "body", "report"]
_KEY_COLUMNS = ["singleton", "body"]
_LEGACY_TABLE = "governed_artifacts"
_ACCESS_TABLE = "governed_artifacts_v2"
_LEGACY_COLUMNS = {
    "artifact_id",
    "task_id",
    "tenant_id",
    "project_id",
    "domains",
    "ref_json",
    "expires_at",
    "held",
    "deleted",
}
_REASON_ORDER = (
    "report_unreadable",
    "legal_hold",
    "evidence_missing",
    "evidence_deleted",
    "evidence_expired",
    "evidence_scope_mismatch",
)


class CsvMaintenanceUnavailable(RuntimeError):
    """The configured state cannot be observed completely; no partial plan is emitted."""


class _ReadOnlyCsvView(CsvUploadStore):
    """Reuse the released record decoder without initializing or opening the store for writes.

    ``CsvUploadStore.__init__`` creates schema and parents when missing. This view deliberately
    skips it: the maintenance operator must point at an existing store, and a read-only SQLite
    connection guarantees the inventory cannot create, extend or repair it.
    """

    def __init__(self, encryption_key: bytes, clock: Callable[[], datetime]):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("csv_intake_requires_aes256_key")
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-csv-intake-scope-v1", "sha256")
        self.clock = clock

    def now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("csv_maintenance_clock_must_be_aware")
        return now


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


@contextmanager
def _read_snapshot(path: Path, code: str):
    if path.is_symlink() or not path.is_file():
        raise CsvMaintenanceUnavailable(code)
    try:
        db = sqlite3.connect(_readonly_uri(path), uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise CsvMaintenanceUnavailable(code) from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db
    except sqlite3.Error as exc:
        raise CsvMaintenanceUnavailable(code) from exc
    finally:
        db.close()


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def load_key_file(path: Path) -> bytes:
    """Read a 32-byte key from an operator-owned private regular file."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CsvMaintenanceUnavailable("csv_maintenance_key_file_invalid")
    metadata = path.stat()
    if hasattr(os, "getuid"):
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise PermissionError("csv_maintenance_key_file_not_private")
        if metadata.st_uid != os.getuid():
            raise PermissionError("csv_maintenance_key_file_owner_mismatch")
    raw = path.read_bytes()
    if not raw or len(raw) > 128:
        raise CsvMaintenanceUnavailable("csv_maintenance_key_file_invalid")
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:
        raise CsvMaintenanceUnavailable("csv_maintenance_key_file_invalid") from exc
    if len(key) != 32:
        raise CsvMaintenanceUnavailable("csv_maintenance_key_file_invalid")
    return key


class CsvMaintenanceInventory:
    def __init__(
        self,
        *,
        store_path: Path,
        encryption_key: bytes,
        artifact_index: Path,
        clock: Callable[[], datetime] | None = None,
        parser_fingerprint: str | None = None,
        processing_fingerprint: str | None = None,
        max_files: int = 200,
    ):
        if type(max_files) is not int or not 1 <= max_files <= 200:
            raise ValueError("invalid_csv_maintenance_inventory_bound")
        self.store_path = Path(store_path)
        self.artifact_index = Path(artifact_index)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.view = _ReadOnlyCsvView(encryption_key, self.clock)
        self.parser_fingerprint = parser_fingerprint or csv_parser.parser_fingerprint(
            csv_parser.CsvLimits()
        )
        self.processing_fingerprint = processing_fingerprint or csv_intake._PROFILE_FINGERPRINT
        self.epoch = canonical_hash(
            {
                "parser": self.parser_fingerprint,
                "processing": self.processing_fingerprint,
            }
        )
        self.max_files = max_files

    def now(self) -> datetime:
        return self.view.now()

    def _read_rows(self) -> list[tuple[sqlite3.Row, object]]:
        code = "csv_maintenance_store_unavailable"
        with _read_snapshot(self.store_path, code) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise CsvMaintenanceUnavailable("csv_maintenance_schema_unsupported")
            if (
                _columns(db, "csv_key") != _KEY_COLUMNS
                or _columns(db, "csv_uploads") != _UPLOAD_COLUMNS
            ):
                raise CsvMaintenanceUnavailable("csv_maintenance_schema_unsupported")
            row = db.execute("SELECT body FROM csv_key WHERE singleton=1").fetchone()
            if row is None:
                raise CsvMaintenanceUnavailable("csv_maintenance_key_missing")
            try:
                canary = self.view.open(row["body"], b"csv-key-v1")
            except InvalidTag as exc:
                raise PermissionError("csv_maintenance_key_mismatch") from exc
            except Exception as exc:
                raise CsvMaintenanceUnavailable(code) from exc
            if canary != b"csv-intake-v1":
                raise PermissionError("csv_maintenance_key_mismatch")
            rows = db.execute(
                "SELECT * FROM csv_uploads ORDER BY rowid LIMIT ?", (self.max_files + 1,)
            ).fetchall()
        if len(rows) > self.max_files:
            raise CsvMaintenanceUnavailable("csv_maintenance_inventory_capacity_invalid")
        return [(row, self._decode(row)) for row in rows]

    def _decode(self, row: sqlite3.Row):
        try:
            return self.view.decode(row)
        except Exception as exc:
            raise CsvMaintenanceUnavailable("csv_maintenance_record_unavailable") from exc

    def _report(self, row: sqlite3.Row, payload):
        try:
            content = self.view.open(row["report"], b"csv-report-v1:" + self.view.aad(row))
            result = CsvProfileResult.model_validate_json(content)
            expected = payload.file.model_copy(update={"status": "profiled"})
            if (
                result.file != expected
                or result.profile.row_count != payload.preview.row_count
                or tuple(column.name for column in result.profile.columns)
                != payload.preview.columns
                or result.profile.connector_id != "csv_upload"
                or result.profile.relation != "uploaded_csv"
                or result.profile_fingerprint != payload.file.processing_fingerprint
                or any(
                    column.data_type != "TEXT"
                    or column.null_count != 0
                    or column.mean is not None
                    or column.row_count != payload.preview.row_count
                    for column in result.profile.columns
                )
                or len({ref.artifact_id for ref in result.evidence}) != len(result.evidence)
                or any(
                    ref.task_id != payload.file.task_id
                    or ref.uri != f"artifact://local/{ref.task_id}/{ref.artifact_id}.json"
                    for ref in result.evidence
                )
            ):
                raise ValueError("csv_maintenance_report_binding_invalid")
            digest = "sha256:" + hashlib.sha256(bytes(row["report"])).hexdigest()
            return (
                tuple(sorted(result.evidence, key=lambda ref: str(ref.artifact_id))),
                digest,
            )
        except Exception:
            return None

    def _entry(self, row: sqlite3.Row, payload, now: datetime, parsed) -> CsvMaintenanceFile:
        record = payload.file
        expired = record.expires_at <= now
        report_state = "absent"
        report_digest = None
        evidence = ()
        reasons = ()
        if row["report"] is not None:
            if parsed is None:
                report_state = "unreadable"
                reasons = ("report_unreadable",)
            else:
                report_state, (evidence, report_digest) = "readable", parsed
        if reasons:
            review_state = "blocked"
        elif expired:
            review_state = "expired_review_required"
        else:
            review_state = "retain"
        return CsvMaintenanceFile(
            file_id=record.file_id,
            task_id=record.task_id,
            owner_ref=self.view.scope(payload.actor)[:16],
            status="profiled" if row["report"] is not None else "staged",
            availability=(
                "compatible"
                if record.parser_fingerprint == self.parser_fingerprint
                and record.processing_fingerprint == self.processing_fingerprint
                else "release_incompatible"
            ),
            source_hash=record.source_hash,
            source_bytes=len(csv_intake.source_bytes(payload)),
            created_at=record.created_at,
            expires_at=record.expires_at,
            expired=expired,
            report_state=report_state,
            report_digest=report_digest,
            evidence_artifact_ids=tuple(ref.artifact_id for ref in evidence),
            blocked_reasons=reasons,
            review_state=review_state,
        )

    def _artifact_reasons(self, actor: ActorContext, evidence, now: datetime) -> tuple[str, ...]:
        with _read_snapshot(
            self.artifact_index, "csv_maintenance_artifact_index_unavailable"
        ) as db:
            for table, expected in (
                (_LEGACY_TABLE, _LEGACY_COLUMNS),
                (_ACCESS_TABLE, _LEGACY_COLUMNS | {"read_policy", "creator_user_id"}),
            ):
                if set(_columns(db, table)) != expected:
                    raise CsvMaintenanceUnavailable("csv_maintenance_artifact_index_unavailable")
            found = set()
            for ref in evidence:
                rows = db.execute(
                    "SELECT 'v2' AS layout, tenant_id, project_id, domains, ref_json, "
                    "expires_at, held, deleted, read_policy, creator_user_id "
                    "FROM governed_artifacts_v2 WHERE artifact_id = ? UNION ALL "
                    "SELECT 'legacy', tenant_id, project_id, domains, ref_json, "
                    "expires_at, held, deleted, 'project', NULL "
                    "FROM governed_artifacts WHERE artifact_id = ?",
                    (str(ref.artifact_id), str(ref.artifact_id)),
                ).fetchall()
                if len(rows) != 1:
                    found.add("evidence_missing")
                    continue
                row = rows[0]
                try:
                    reference = type(ref).model_validate_json(row["ref_json"])
                    domains = set(json.loads(row["domains"]))
                except Exception:
                    found.add("evidence_scope_mismatch")
                    continue
                if (
                    reference != ref
                    or (row["tenant_id"], row["project_id"]) != (actor.tenant_id, actor.project_id)
                    or not domains <= set(actor.data_domains)
                    or row["read_policy"] not in ("project", "creator")
                    or (row["layout"] == "v2" and not row["creator_user_id"])
                    or (row["read_policy"] == "creator" and row["creator_user_id"] != actor.user_id)
                ):
                    found.add("evidence_scope_mismatch")
                if row["held"]:
                    found.add("legal_hold")
                if row["deleted"]:
                    found.add("evidence_deleted")
                if row["expires_at"] <= now.timestamp():
                    found.add("evidence_expired")
            return tuple(reason for reason in _REASON_ORDER if reason in found)

    def inventory(self) -> CsvMaintenancePlan:
        now = self.now()
        decoded = self._read_rows()
        entries = []
        for row, payload in decoded:
            parsed = self._report(row, payload) if row["report"] is not None else None
            entry = self._entry(row, payload, now, parsed)
            if entry.expired and parsed is not None:
                reasons = self._artifact_reasons(payload.actor, parsed[0], now)
                if reasons:
                    entry = entry.model_copy(
                        update={"blocked_reasons": reasons, "review_state": "blocked"}
                    )
            entries.append(entry)
        files = tuple(sorted(entries, key=lambda item: (item.created_at, str(item.file_id))))
        summary = CsvMaintenanceSummary(
            retained_files=len(files),
            expired_files=sum(item.expired for item in files),
            review_required_files=sum(
                item.review_state == "expired_review_required" for item in files
            ),
            blocked_files=sum(item.review_state == "blocked" for item in files),
            staged_files=sum(item.status == "staged" for item in files),
            profiled_files=sum(item.status == "profiled" for item in files),
            retained_source_bytes=sum(item.source_bytes for item in files),
            expired_source_bytes=sum(item.source_bytes for item in files if item.expired),
        )
        return CsvMaintenancePlan(
            observed_at=now,
            epoch=self.epoch,
            inventory_digest=maintenance_digest(self.epoch, files),
            status="blocked" if summary.blocked_files else "complete",
            summary=summary,
            files=files,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only retained CSV inventory for reviewed maintenance (never deletes)."
    )
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--artifact-index", required=True, type=Path)
    parser.add_argument("--summary", action="store_true")
    try:
        args = parser.parse_args(argv)
        plan = CsvMaintenanceInventory(
            store_path=args.store,
            encryption_key=load_key_file(args.key_file),
            artifact_index=args.artifact_index,
        ).inventory()
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CsvMaintenanceUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 4
    output = (
        {
            "status": plan.status,
            "observed_at": plan.observed_at.isoformat(),
            "inventory_digest": plan.inventory_digest,
            "summary": plan.summary.model_dump(mode="json"),
        }
        if args.summary
        else plan.model_dump(mode="json")
    )
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 3 if plan.status == "blocked" else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
