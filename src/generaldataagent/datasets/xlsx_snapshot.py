"""Derived, creator-scoped retention of declared XLSX column values; never returned.

The snapshot stores canonical values encrypted inside the existing payload after the extracted
text passes DLP. This endpoint only returns counts and hashes: there is no value read path, no
artifact registration and no `DatasetSnapshot` claim.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets import xlsx_preview as reader
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    MAX_DECLARED_HEADER_CHARS,
    MAX_SNAPSHOT_CELLS,
    MAX_SNAPSHOT_CHARS,
    SNAPSHOT_STATUS_CODES,
    DeclaredColumnType,
    XlsxIntakeLimit,
    XlsxInterpretation,
    XlsxSnapshotColumnData,
    XlsxSnapshotData,
    declared_header_labels,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict
from generaldataagent.datasets.xlsx_interpretation import interpret_cell
from generaldataagent.datasets.xlsx_preflight import MAIN, MAX_ROWS
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS, CellKind, XlsxPreviewData

SCAN_CHUNK_CHARS = 256 * 1024


class XlsxSnapshotColumn(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    type: DeclaredColumnType
    cells: int = Field(ge=0, le=MAX_ROWS, strict=True)
    values: int = Field(ge=0, strict=True)
    missing: int = Field(ge=0, strict=True)
    unparsed: int = Field(ge=0, strict=True)
    not_trusted: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_are_complete(self) -> "XlsxSnapshotColumn":
        if self.values + self.missing + self.unparsed + self.not_trusted != self.cells:
            raise ValueError("xlsx_snapshot_counts_invalid")
        return self


class XlsxSnapshotResult(ContractModel):
    """Metadata for a retained derived snapshot; values are never returned by this endpoint."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-derived-snapshot-v1"] = "gda-xlsx-derived-snapshot-v1"
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: AwareDatetime
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    date_system: Literal["1900", "1904"]
    data_rows: int = Field(ge=0, le=MAX_ROWS, strict=True)
    cells: int = Field(ge=0, strict=True)
    columns: tuple[XlsxSnapshotColumn, ...] = Field(max_length=PREVIEW_COLUMNS)
    duplicate_rows: Literal["reject", "keep-all"]
    duplicate_row_count: int = Field(ge=0, le=MAX_ROWS, strict=True)
    status: Literal["current", "stale"]
    scope: Literal["whole-sheet-declared-columns"] = "whole-sheet-declared-columns"
    rows: Literal["present-rows-only"] = "present-rows-only"
    values: Literal["retained-encrypted-not-returned"] = "retained-encrypted-not-returned"
    storage: Literal["encrypted-derived-not-artifact"] = "encrypted-derived-not-artifact"
    dlp: Literal["extracted-values-scanned"] = "extracted-values-scanned"
    formula_cached_values: Literal["never-trusted"] = "never-trusted"
    missing_tokens: Literal["literal-exact-match"] = "literal-exact-match"
    currencies: Literal["not-interpreted"] = "not-interpreted"
    percentages: Literal["not-interpreted"] = "not-interpreted"
    times_of_day: Literal["not-interpreted"] = "not-interpreted"
    duplicate_rows_handling: Literal["counted-not-dropped"] = "counted-not-dropped"
    dataset_snapshot: Literal[False] = False
    artifact: Literal[False] = False

    @model_validator(mode="after")
    def report_is_complete(self) -> "XlsxSnapshotResult":
        if any(column.cells != self.data_rows for column in self.columns):
            raise ValueError("xlsx_snapshot_counts_invalid")
        if self.cells != self.data_rows * len(self.columns):
            raise ValueError("xlsx_snapshot_counts_invalid")
        if self.duplicate_row_count > self.data_rows:
            raise ValueError("xlsx_snapshot_counts_invalid")
        return self


def derive_snapshot(
    data: bytes,
    *,
    preview: XlsxPreviewData,
    interpretation: XlsxInterpretation,
    date_system: str,
    created_at,
    check: Callable[[], None],
) -> XlsxSnapshotData:
    """Read every present row of the declared columns and retain canonical values by column."""
    if date_system not in ("1900", "1904"):
        raise ValueError("xlsx_date_system_invalid")
    labels = declared_header_labels(preview, interpretation.header_row)
    if labels is None or any(
        column.index >= len(labels) or column.header != labels[column.index]
        for column in interpretation.columns
    ):
        raise ValueError("xlsx_interpretation_binding_invalid")
    if not interpretation.columns:
        raise ValueError("xlsx_snapshot_columns_required")
    position = preview.row_numbers.index(interpretation.header_row)
    expected_rows = preview.row_count - position - 1
    worksheet, shared = reader._sheet_part(data, preview.sheet)
    root = reader._xml(worksheet, "xlsx_snapshot_sheet_invalid")
    if root.tag != f"{{{MAIN}}}worksheet":
        raise ValueError("xlsx_snapshot_sheet_invalid")
    declared = interpretation.columns
    wanted = {column.index + 1 for column in declared}
    statuses: list[list[str]] = [[] for _ in declared]
    values: list[list[str]] = [[] for _ in declared]
    row_numbers: list[int] = []
    seen: set[str] = set()
    duplicate_row_count = 0
    characters = 0
    digest = hashlib.sha256(b"gda-xlsx-derived-snapshot-v1\n")
    digest.update(interpretation.interpretation_hash.encode() + b"\n")
    previous_row = 0
    header_found = False
    for element in root.findall(f".//{{{MAIN}}}row"):
        row_number = reader._row_number(element.get("r"), previous_row)
        previous_row = row_number
        if not header_found and row_number != interpretation.header_row:
            continue
        if not header_found:
            header_found = True
            continue
        if len(row_numbers) % 256 == 1:
            check()
        if (len(row_numbers) + 1) * len(declared) > MAX_SNAPSHOT_CELLS:
            raise XlsxIntakeLimit("xlsx_snapshot_cell_limit")
        cells = _row_cells(element, row_number, wanted, shared)
        readings = tuple(
            interpret_cell(
                row=row_number,
                raw=cells.get(column.index + 1, ("", "empty"))[0],
                kind=cells.get(column.index + 1, ("", "empty"))[1],
                declaration=column,
                date_system=date_system,
            )
            for column in declared
        )
        characters += sum(len(cell.value or "") for cell in readings)
        if characters > MAX_SNAPSHOT_CHARS:
            raise XlsxIntakeLimit("xlsx_snapshot_char_limit")
        row_numbers.append(row_number)
        row_key = json.dumps(
            [[cell.status, cell.value] for cell in readings],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        duplicate_row_count += row_key in seen
        seen.add(row_key)
        digest.update(row_key.encode() + b"\n")
        for index, cell in enumerate(readings):
            statuses[index].append(SNAPSHOT_STATUS_CODES[cell.status])
            if cell.value is not None:
                values[index].append(cell.value)
    if not header_found or len(row_numbers) != expected_rows:
        raise ValueError("xlsx_snapshot_binding_invalid")
    return XlsxSnapshotData(
        snapshot_hash="sha256:" + digest.hexdigest(),
        source_hash=interpretation.source_hash,
        preview_hash=interpretation.preview_hash,
        confirmation_hash=interpretation.confirmation_hash,
        interpretation_hash=interpretation.interpretation_hash,
        created_at=created_at,
        header_row=interpretation.header_row,
        date_system=date_system,
        rows=tuple(row_numbers),
        columns=tuple(
            XlsxSnapshotColumnData(
                index=column.index,
                header=column.header,
                type=column.type,
                statuses="".join(statuses[index]),
                values=tuple(values[index]),
            )
            for index, column in enumerate(declared)
        ),
        duplicate_rows=interpretation.duplicate_rows,
        duplicate_row_count=duplicate_row_count,
    )


def scan_snapshot_values(snapshot: XlsxSnapshotData, scanner, check: Callable[[], None]) -> None:
    """Scan every retained value before persistence; denial fails the whole derivation."""
    chunk: list[str] = []
    size = 0
    for column in snapshot.columns:
        for value in column.values:
            chunk.append(value)
            size += len(value) + 1
            if size >= SCAN_CHUNK_CHARS:
                check()
                scanner.check("\n".join(chunk).encode())
                chunk, size = [], 0
    if chunk:
        check()
        scanner.check("\n".join(chunk).encode())


def snapshot_result(
    snapshot: XlsxSnapshotData, interpretation: XlsxInterpretation
) -> XlsxSnapshotResult:
    counts = []
    for column in snapshot.columns:
        counter = Counter(column.statuses)
        counts.append(
            XlsxSnapshotColumn(
                index=column.index,
                header=column.header,
                type=column.type,
                cells=len(snapshot.rows),
                values=counter["v"],
                missing=counter["m"],
                unparsed=counter["u"],
                not_trusted=counter["t"],
            )
        )
    current = interpretation.interpretation_hash == snapshot.interpretation_hash
    return XlsxSnapshotResult(
        snapshot_hash=snapshot.snapshot_hash,
        source_hash=snapshot.source_hash,
        preview_hash=snapshot.preview_hash,
        confirmation_hash=snapshot.confirmation_hash,
        interpretation_hash=snapshot.interpretation_hash,
        created_at=snapshot.created_at,
        header_row=snapshot.header_row,
        date_system=snapshot.date_system,
        data_rows=len(snapshot.rows),
        cells=len(snapshot.rows) * len(snapshot.columns),
        columns=tuple(counts),
        duplicate_rows=snapshot.duplicate_rows,
        duplicate_row_count=snapshot.duplicate_row_count,
        status="current" if current else "stale",
    )


def _row_cells(
    element, row_number: int, wanted: set[int], shared: tuple[str, ...]
) -> dict[int, tuple[str, CellKind]]:
    """Materialise only declared columns; every other cell stays uninspected."""
    cells: dict[int, tuple[str, CellKind]] = {}
    previous_column = 0
    for cell in element.findall(f"{{{MAIN}}}c"):
        parsed = reader._cell_reference(cell.get("r"))
        if parsed is None:
            column = previous_column + 1
        else:
            column, cell_row = parsed
            if cell_row != row_number:
                raise ValueError("xlsx_snapshot_sheet_invalid")
        previous_column = column
        if column in wanted:
            text, _, _, kind = reader._cell(cell, shared, True)
            cells[column] = (text, kind)
    return cells


class XlsxSnapshotWriter:
    def __init__(self, intake: XlsxIntakeService):
        self.intake = intake

    def derive(
        self, file_id: UUID, actor: ActorContext, token: str, *, deadline: float | None = None
    ) -> XlsxSnapshotResult:
        """Derive and retain the snapshot; replay returns the same stored record without a write."""
        check = self.intake.guard(actor, token, deadline=deadline)
        with self.intake.store.db(write=True) as db:
            row, payload, raw = self.intake.store.owned(db, file_id, actor)
            bound = self.intake._bound_guard(payload, check)
            interpretation = payload.interpretation
            if interpretation is None:
                raise XlsxIntakeConflict("xlsx_interpretation_not_declared")
            if not interpretation.columns:
                raise XlsxIntakeConflict("xlsx_snapshot_no_declared_columns")
            self.intake._parsed(payload, raw, bound)
            snapshot = derive_snapshot(
                raw,
                preview=payload.preview,
                interpretation=interpretation,
                date_system=payload.preflight.date_system,
                created_at=self.intake.store.now(),
                check=bound,
            )
            if payload.snapshot is not None and payload.snapshot.snapshot_hash == (
                snapshot.snapshot_hash
            ):
                bound()
                return snapshot_result(payload.snapshot, interpretation)
            scan_snapshot_values(snapshot, self.intake.scanner, bound)
            self.intake._audit(
                "xlsx.snapshot_derived",
                payload,
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "interpretation_hash": snapshot.interpretation_hash,
                    "data_rows": len(snapshot.rows),
                    "cells": len(snapshot.rows) * len(snapshot.columns),
                },
            )
            updated = payload.model_copy(update={"snapshot": snapshot})
            encrypted = self.intake.store.seal(
                updated.model_dump_json().encode(), self.intake.store.body_aad(row)
            )
            db.execute("UPDATE xlsx_uploads SET body=? WHERE file_id=?", (encrypted, str(file_id)))
            bound()
            return snapshot_result(snapshot, interpretation)

    def read(
        self, file_id: UUID, actor: ActorContext, token: str, *, deadline: float | None = None
    ) -> XlsxSnapshotResult:
        """Return stored snapshot metadata only; no value, no re-derivation of values."""
        check = self.intake.guard(actor, token, deadline=deadline)
        with self.intake.store.db() as db:
            _, payload, raw = self.intake.store.owned(db, file_id, actor)
            bound = self.intake._bound_guard(payload, check)
            if payload.snapshot is None:
                raise XlsxIntakeConflict("xlsx_snapshot_not_derived")
            self.intake._parsed(payload, raw, bound)
            bound()
            return snapshot_result(payload.snapshot, payload.interpretation)


__all__ = [
    "MAX_SNAPSHOT_CELLS",
    "MAX_SNAPSHOT_CHARS",
    "XlsxSnapshotColumn",
    "XlsxSnapshotResult",
    "XlsxSnapshotWriter",
    "derive_snapshot",
    "scan_snapshot_values",
    "snapshot_result",
]
