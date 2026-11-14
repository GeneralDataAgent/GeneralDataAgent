"""Explicit, identity-bound CSV export of one approved declared XLSX column page.

This local operator port never chooses an identity, columns or rows for the caller. It consumes one
one-time R2 approval bound to the exact creator/tenant/project/domain scope, retained snapshot hash,
deterministic instance-bound export hash, sorted declared-column selection and bounded keyset row
range. It observes the existing encrypted staging database read-only with the released decoder,
derives one CSV page from the retained snapshot, re-scans the entire output with DLP, emits a
content-free audit event and consumes the grant. The CLI writes one new operator-owned output file;
the port never writes a source row, artifact index or evidence record.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
from bisect import bisect_right
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import Literal, Protocol
from urllib.parse import quote
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import AwareDatetime, Field, StrictInt, model_validator

from generaldataagent.contracts import (
    ActorContext,
    ArtifactClassification,
    AuditEvent,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.contracts.approval import ApprovalRequest
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_intake_contracts import (
    XlsxInterpretation,
    XlsxLimits,
    XlsxSnapshotColumnData,
    XlsxSnapshotData,
    _XlsxPayload,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeUnavailable
from generaldataagent.datasets.xlsx_maintenance import (
    XlsxMaintenanceUnavailable,
    _columns,
    _ReadOnlyXlsxView,
    load_key_file,
    load_token_file,
)
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS
from generaldataagent.datasets.xlsx_snapshot import MAX_PAGE_CHARS, MAX_PAGE_ROWS
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.policy.approvals import (
    ApprovalError,
    SQLiteApprovalStore,
    request_fingerprint,
)
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker, SQLiteSessionBroker

_TOOL_NAME = "xlsx.export.columns"
_ACTION = "export-declared-columns"
_TABLE_COLUMNS = ["file_id", "scope", "idem", "request_hash", "body", "source"]
_ARGUMENT_KEYS = frozenset({"export_hash", "file_ids", "columns", "after", "limit"})


class AuditSink(Protocol):
    """Append-only event sink; unavailable audit must deny the export."""

    def emit(self, event: AuditEvent) -> None: ...


class XlsxExportRefused(PermissionError):
    """The approval or current authorization does not permit this export."""


class XlsxExportUnavailable(RuntimeError):
    """The retained store, DLP scanner or audit cannot be observed safely."""


class XlsxExportConflict(RuntimeError):
    """The retained state contradicts the approved export."""


class XlsxExportRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    columns: tuple[StrictInt, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)
    after: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=MAX_PAGE_ROWS, ge=1, le=MAX_PAGE_ROWS)
    instance_id: UUID

    @model_validator(mode="after")
    def columns_are_sorted_unique(self) -> "XlsxExportRequest":
        if any(type(index) is not int for index in self.columns) or any(
            not 0 <= index < PREVIEW_COLUMNS or (position and index <= self.columns[position - 1])
            for position, index in enumerate(self.columns)
        ):
            raise ValueError("xlsx_export_columns_invalid")
        instance_id = getattr(self, "instance_id", None)
        if instance_id is not None and type(instance_id) is not UUID:
            raise ValueError("xlsx_export_instance_id_invalid")
        return self

    def tool_request(self, actor: ActorContext, task_id: UUID) -> ToolRequest:
        """One exact ToolRequest; the approval registry binds its full canonical fingerprint."""
        return ToolRequest(
            task_id=task_id,
            user_id=actor.user_id,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            agent_role="data_engineer",
            tool_name=_TOOL_NAME,
            action=_ACTION,
            resource=self.export_hash,
            arguments=self.arguments,
            data_domains=actor.data_domains,
            requested_risk=RiskLevel.R2,
        )

    @property
    def arguments(self) -> dict[str, object]:
        return {
            "export_hash": self.export_hash,
            "file_ids": [str(self.file_id)],
            "columns": list(self.columns),
            "after": self.after,
            "limit": self.limit,
        }

    @property
    def export_hash(self) -> str:
        return canonical_hash(
            {
                "stage": "xlsx-explicit-column-export-v1",
                "file_id": str(self.file_id),
                "snapshot_hash": self.snapshot_hash,
                "columns": list(self.columns),
                "after": self.after,
                "limit": self.limit,
                "instance_id": str(self.instance_id),
            }
        )


class XlsxExportReceipt(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    export_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: UUID
    columns: tuple[int, ...]
    rows: int = Field(ge=0, le=MAX_PAGE_ROWS, strict=True)
    cells: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, le=MAX_PAGE_CHARS, strict=True)
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: AwareDatetime
    scope: Literal["explicit-declared-column-page"] = "explicit-declared-column-page"
    values: Literal["selected-columns-only"] = "selected-columns-only"
    dlp: Literal["rescanned-before-return"] = "rescanned-before-return"
    formula_cached_values: Literal["never-trusted"] = "never-trusted"
    artifact: Literal[False] = False
    dataset_snapshot: Literal[False] = False
    audit: Literal["xlsx.columns_exported"] = "xlsx.columns_exported"
    approval: Literal["one-time-grant-consumed"] = "one-time-grant-consumed"

    @model_validator(mode="after")
    def receipt_is_coherent(self) -> "XlsxExportReceipt":
        if self.cells != self.rows * len(self.columns) or not self.columns:
            raise ValueError("xlsx_export_receipt_invalid")
        return self


def _read_uri(path: Path) -> str:
    return "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"


def _formula_safe(text: str) -> str:
    """Neutralize spreadsheet formula prefixes while preserving the canonical text after them."""
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def _snapshot_content_hash(snapshot: XlsxSnapshotData, check: Callable[[], None]) -> str:
    """Bind the retained snapshot projection to the released whole-reading digest."""
    status_map = {"v": "value", "m": "missing", "u": "unparsed", "t": "not_trusted"}
    digest = hashlib.sha256(b"gda-xlsx-derived-snapshot-v1\n")
    digest.update(snapshot.interpretation_hash.encode() + b"\n")
    values = [iter(column.values) for column in snapshot.columns]
    for position in range(len(snapshot.rows)):
        if position % 256 == 0:
            check()
        readings = []
        for column, remaining in zip(snapshot.columns, values, strict=True):
            status = column.statuses[position]
            readings.append([status_map[status], next(remaining) if status == "v" else None])
        digest.update(
            json.dumps(readings, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        )
    return "sha256:" + digest.hexdigest()


def _csv_content(
    snapshot: XlsxSnapshotData,
    request: XlsxExportRequest,
    check: Callable[[], None],
) -> tuple[bytes, int]:
    selected: list[XlsxSnapshotColumnData] = []
    if snapshot.snapshot_hash != _snapshot_content_hash(snapshot, check):
        raise XlsxExportConflict("xlsx_export_snapshot_changed")
    for index in request.columns:
        matches = [column for column in snapshot.columns if column.index == index]
        if len(matches) != 1:
            raise XlsxExportConflict("xlsx_export_column_not_declared")
        selected.append(matches[0])
    if any(len(column.statuses) != len(snapshot.rows) for column in selected):
        raise XlsxExportConflict("xlsx_export_snapshot_counts_invalid")
    start = 0 if request.after == 0 else bisect_right(snapshot.rows, request.after)
    stop = min(start + request.limit, len(snapshot.rows))
    offsets = [column.statuses.count("v", 0, start) for column in selected]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow([_formula_safe(column.header) for column in selected])
    characters = 0
    for position in range(start, stop):
        cells: list[str] = []
        for offset, column in enumerate(selected):
            if column.statuses[position] != "v":
                cells.append("")
                continue
            text = column.values[offsets[offset]]
            offsets[offset] += 1
            characters += len(text)
            cells.append(_formula_safe(text))
        if characters > MAX_PAGE_CHARS:
            raise XlsxExportUnavailable("xlsx_export_page_char_limit")
        check()
        writer.writerow(cells)
    content = output.getvalue().encode("utf-8")
    if len(content) > MAX_PAGE_CHARS:
        raise XlsxExportUnavailable("xlsx_export_page_char_limit")
    return content, stop - start


def _scan_content(content: bytes, scanner: TextDLPScanner, check: Callable[[], None]) -> None:
    """Scan the exact CSV bytes, including headers, quotes and formula-neutralized cells."""
    check()
    try:
        scanner.check_export(content, ArtifactClassification.INTERNAL)
    except PermissionError:
        raise
    except Exception as exc:
        raise XlsxExportUnavailable("xlsx_export_scan_unavailable") from exc
    check()


class XlsxColumnExport:
    def __init__(
        self,
        *,
        store_path: Path,
        encryption_key: bytes,
        approval_db: Path,
        approval_id: UUID,
        actor: ActorContext,
        session_token: str,
        broker: SessionBroker,
        audit: AuditSink,
        request: XlsxExportRequest,
        clock: Callable[[], datetime] | None = None,
        deadlines: Callable[[], float] = monotonic,
        limits: XlsxLimits | None = None,
        scanner: TextDLPScanner | None = None,
    ):
        self.path, self.approval_id, self.request = store_path, approval_id, request
        self.actor = canonical_actor(actor)
        self.session_token, self.broker, self.audit = session_token, broker, audit
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("xlsx_export_requires_audit")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.deadlines = deadlines
        self.limits = limits or XlsxLimits()
        self.scanner = scanner or TextDLPScanner()
        self.view = _ReadOnlyXlsxView(encryption_key, self.clock)
        self.approvals = SQLiteApprovalStore(approval_db, clock=self.clock)

    def _guard(self) -> Callable[[], None]:
        reference = self.broker.session_reference(self.session_token)
        end = self.deadlines() + self.limits.max_seconds

        def check() -> None:
            if self.deadlines() >= end:
                raise XlsxExportUnavailable("xlsx_operation_time_limit")
            identity = self.broker.authenticate_reference(reference)
            if (
                identity.business_constraint is not None
                or "analysis:run" not in identity.permissions
                or (identity.user_id, identity.tenant_id, identity.project_id)
                != (self.actor.user_id, self.actor.tenant_id, self.actor.project_id)
                or not set(self.actor.data_domains) <= identity.data_domains
            ):
                raise XlsxExportRefused("xlsx_export_authorization_unavailable")

        check()
        return check

    def _approval(self, check: Callable[[], None]) -> ApprovalRequest:
        """Bind the grant to the full ToolRequest, not only a caller-selected argument subset."""
        try:
            record = self.approvals.inspect(self.approval_id)
        except ApprovalError as exc:
            raise XlsxExportRefused("xlsx_export_approval_unknown") from exc
        approved = record.request
        tool_request = self.request.tool_request(self.actor, approved.task_id)
        expected = request_fingerprint(tool_request)
        if (
            approved.tool_name != _TOOL_NAME
            or approved.action != _ACTION
            or record.grant is None
            or record.disposition is not None
            or record.consumed
            or approved.resource != self.request.export_hash
            or set(approved.parameters) != _ARGUMENT_KEYS
            or approved.parameters != self.request.arguments
            or approved.request_fingerprint != expected
            or record.grant.request_fingerprint != expected
            or record.grant.approved_at > self.clock()
            or record.grant.expires_at <= self.clock()
            or approved.expires_at <= self.clock()
            or approved.classification != ArtifactClassification.INTERNAL
            or approved.data_domains != self.actor.data_domains
        ):
            raise XlsxExportRefused("xlsx_export_approval_unavailable")
        check()
        return approved

    def _observe(self, check: Callable[[], None]) -> tuple[_XlsxPayload, bytes]:
        if self.path.is_symlink() or not self.path.is_file():
            raise XlsxExportUnavailable("xlsx_export_store_unavailable")
        try:
            with closing(sqlite3.connect(_read_uri(self.path), uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise XlsxExportUnavailable("xlsx_export_layout_unsupported")
                if (
                    _columns(db, "xlsx_key") != ["singleton", "body"]
                    or _columns(db, "xlsx_uploads") != _TABLE_COLUMNS
                ):
                    raise XlsxExportUnavailable("xlsx_export_layout_unsupported")
                canary = db.execute("SELECT body FROM xlsx_key WHERE singleton=1").fetchone()
                if canary is None:
                    raise XlsxExportUnavailable("xlsx_export_key_missing")
                try:
                    opened = self.view.open(canary[0], b"xlsx-key-v1")
                except InvalidTag as exc:
                    raise XlsxExportRefused("xlsx_export_key_mismatch") from exc
                except Exception as exc:
                    raise XlsxExportUnavailable("xlsx_export_store_unreadable") from exc
                if opened != b"xlsx-intake-v1":
                    raise XlsxExportRefused("xlsx_export_key_mismatch")
                row = db.execute(
                    "SELECT * FROM xlsx_uploads WHERE file_id=?", (str(self.request.file_id),)
                ).fetchone()
                if row is None:
                    raise XlsxExportConflict("xlsx_export_source_missing")
                if row["scope"] != self.view.scope(self.actor):
                    raise XlsxExportRefused("xlsx_export_scope_unavailable")
                try:
                    payload, raw = self.view.decode(row)
                except Exception as exc:
                    raise XlsxExportUnavailable("xlsx_export_record_unreadable") from exc
        except (XlsxExportRefused, XlsxExportUnavailable, XlsxExportConflict, PermissionError):
            raise
        except (sqlite3.Error, InvalidTag) as exc:
            raise XlsxExportUnavailable("xlsx_export_store_unreadable") from exc
        except Exception as exc:
            raise XlsxExportUnavailable("xlsx_export_layout_unsupported") from exc
        if payload.actor != self.actor:
            raise XlsxExportRefused("xlsx_export_file_unavailable")
        check()
        return payload, raw

    def _bind(
        self,
        payload: _XlsxPayload,
        raw: bytes,
        check: Callable[[], None],
    ) -> tuple[XlsxInterpretation, XlsxSnapshotData]:
        file, interpretation, snapshot = (
            payload.file,
            payload.interpretation,
            payload.snapshot,
        )
        check()
        if (
            interpretation is None
            or snapshot is None
            or file.status != "confirmed"
            or snapshot.snapshot_hash != self.request.snapshot_hash
            or snapshot.interpretation_hash != interpretation.interpretation_hash
            or snapshot.source_hash != file.source_hash
            or snapshot.preview_hash != file.preview_hash
            or snapshot.confirmation_hash != file.confirmation_hash
            or file.parser_fingerprint != xlsx_intake.parser_fingerprint(self.limits, file.config)
            or file.processing_fingerprint != xlsx_intake._PROCESSING_FINGERPRINT
            or file.source_hash != "sha256:" + hashlib.sha256(raw).hexdigest()
            or file.byte_count != len(raw)
        ):
            raise XlsxExportConflict("xlsx_export_snapshot_changed")
        selected = {column.index: column.header for column in interpretation.columns}
        projected = {column.index: column.header for column in snapshot.columns}
        if any(
            index not in selected or selected[index] != projected.get(index)
            for index in self.request.columns
        ):
            raise XlsxExportConflict("xlsx_export_column_not_declared")
        check()
        return interpretation, snapshot

    def _audit_export(
        self,
        approved: ApprovalRequest,
        content: bytes,
        rows: int,
        check: Callable[[], None],
    ) -> None:
        """Emit content-free audit before returning data; audit failure fails the export closed."""
        check()
        event = AuditEvent(
            event_type="xlsx.columns_exported",
            task_id=approved.task_id,
            actor_id=self.actor.user_id,
            tenant_id=self.actor.tenant_id,
            trace_id=str(approved.task_id),
            occurred_at=self.clock(),
            payload={
                "file_id": str(self.request.file_id),
                "export_hash": self.request.export_hash,
                "snapshot_hash": self.request.snapshot_hash,
                "content_sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "rows": rows,
                "cells": rows * len(self.request.columns),
                "bytes": len(content),
                "columns": len(self.request.columns),
            },
        )
        try:
            self.audit.emit(event)
        except Exception as exc:
            raise XlsxExportUnavailable("xlsx_export_audit_unavailable") from exc
        check()

    def execute(self) -> tuple[bytes, XlsxExportReceipt]:
        return self._execute()

    def _execute(
        self, stage_output: Callable[[bytes], None] | None = None
    ) -> tuple[bytes, XlsxExportReceipt]:
        authorize = self._guard()
        approved = self._approval(authorize)
        payload, raw = self._observe(authorize)

        def check() -> None:
            authorize()
            if payload.file.expires_at <= self.clock():
                raise XlsxExportRefused("xlsx_export_file_unavailable")

        interpretation, snapshot = self._bind(payload, raw, check)
        content, rows = _csv_content(snapshot, self.request, check)
        check()
        _scan_content(content, self.scanner, check)
        if stage_output is not None:
            stage_output(content)
        self._audit_export(approved, content, rows, check)
        check()
        try:
            self.approvals.consume_granted(self.approval_id)
        except ApprovalError as exc:
            raise XlsxExportRefused("xlsx_export_approval_unavailable") from exc
        check()
        receipt = XlsxExportReceipt(
            file_id=self.request.file_id,
            snapshot_hash=snapshot.snapshot_hash,
            interpretation_hash=interpretation.interpretation_hash,
            export_hash=self.request.export_hash,
            approval_id=self.approval_id,
            columns=self.request.columns,
            rows=rows,
            cells=rows * len(self.request.columns),
            bytes=len(content),
            content_sha256="sha256:" + hashlib.sha256(content).hexdigest(),
            created_at=self.clock(),
        )
        return content, receipt


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export an approved XLSX declared-column page")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--approval-db", type=Path, required=True)
    parser.add_argument("--approval-id", type=UUID, required=True)
    parser.add_argument("--sessions-db", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--data-domain", action="append", required=True)
    parser.add_argument("--file-id", type=UUID, required=True)
    parser.add_argument("--snapshot-hash", required=True)
    parser.add_argument("--columns", required=True, help="comma-separated declared column indices")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=MAX_PAGE_ROWS)
    parser.add_argument("--instance-id", type=UUID, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _publish_private(staged: Path, path: Path) -> None:
    try:
        os.link(staged, path)
    except FileExistsError as exc:
        raise XlsxExportConflict("xlsx_export_output_exists") from exc


def _export_to_path(export: XlsxColumnExport, path: Path) -> XlsxExportReceipt:
    staged: Path | None = None

    def stage(content: bytes) -> None:
        nonlocal staged
        with NamedTemporaryFile(dir=path.parent, prefix=".gda-xlsx-", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise XlsxExportConflict("xlsx_export_output_exists")

    try:
        _, receipt = export._execute(stage)
        assert staged is not None
        try:
            _publish_private(staged, path)
        except (OSError, XlsxExportConflict) as exc:
            raise XlsxExportUnavailable("xlsx_export_publish_failed_after_consumption") from exc
        return receipt
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.out.exists() or args.out.is_symlink():
            raise XlsxExportConflict("xlsx_export_output_exists")
        columns = tuple(int(part) for part in args.columns.split(","))
        actor = ActorContext(
            user_id=args.user_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            data_domains=tuple(args.data_domain),
        )
        request = XlsxExportRequest(
            file_id=args.file_id,
            snapshot_hash=args.snapshot_hash,
            columns=columns,
            after=args.after,
            limit=args.limit,
            instance_id=args.instance_id,
        )
        export = XlsxColumnExport(
            store_path=args.store,
            encryption_key=load_key_file(args.key_file),
            approval_db=args.approval_db,
            approval_id=args.approval_id,
            actor=actor,
            session_token=load_token_file(args.token_file),
            broker=SQLiteSessionBroker(args.sessions_db),
            audit=JsonlAuditSink(args.audit_log),
            request=request,
        )
        receipt = _export_to_path(export, args.out)
        print(receipt.model_dump_json())
        return 0
    except (XlsxExportRefused, PermissionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except XlsxExportConflict as exc:
        print(str(exc), file=sys.stderr)
        return 5
    except (
        XlsxExportUnavailable,
        XlsxIntakeUnavailable,
        XlsxMaintenanceUnavailable,
        OSError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"xlsx_export_unavailable: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
