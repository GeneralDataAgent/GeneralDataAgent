"""Bounded whole-sheet conformance of declared XLSX columns: counts and digests only.

The report reads every present row below the declared header, restricts reads to declared
columns, and never returns cell text, so no new DLP surface is exposed. Readings use the same
deterministic interpretation as the preview-window endpoint; formula caches stay untrusted.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets import xlsx_preview as reader
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    MAX_DECLARED_HEADER_CHARS,
    DeclaredColumnType,
    XlsxIntakeLimit,
    XlsxInterpretation,
    declared_header_labels,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict
from generaldataagent.datasets.xlsx_interpretation import InterpretationReason, interpret_cell
from generaldataagent.datasets.xlsx_preflight import MAIN, MAX_ROWS
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS, CellKind, XlsxPreviewData

MAX_CONFORMANCE_CELLS = 250_000
MAX_CONFORMANCE_CHARS = 16 * 1024 * 1024


class XlsxReasonCount(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    reason: InterpretationReason
    count: int = Field(ge=1, strict=True)


class XlsxColumnConformance(ContractModel):
    """Per-column reading counts over every present data row; no cell text is returned."""

    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    type: DeclaredColumnType
    cells: int = Field(ge=0, le=MAX_ROWS, strict=True)
    values: int = Field(ge=0, strict=True)
    missing: int = Field(ge=0, strict=True)
    unparsed: int = Field(ge=0, strict=True)
    not_trusted: int = Field(ge=0, strict=True)
    reasons: tuple[XlsxReasonCount, ...] = Field(max_length=16)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def counts_are_complete(self) -> "XlsxColumnConformance":
        if self.values + self.missing + self.unparsed + self.not_trusted != self.cells:
            raise ValueError("xlsx_conformance_counts_invalid")
        names = [item.reason for item in self.reasons]
        if names != sorted(set(names)):
            raise ValueError("xlsx_conformance_reasons_invalid")
        if sum(item.count for item in self.reasons) != self.cells - self.values:
            raise ValueError("xlsx_conformance_reasons_invalid")
        return self


class XlsxConformanceResult(ContractModel):
    """Derived whole-sheet reading of the declared columns; nothing is persisted or audited."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-declared-column-conformance-v1"] = (
        "gda-xlsx-declared-column-conformance-v1"
    )
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    date_system: Literal["1900", "1904"]
    data_rows: int = Field(ge=0, le=MAX_ROWS, strict=True)
    cells_read: int = Field(ge=0, strict=True)
    columns: tuple[XlsxColumnConformance, ...] = Field(max_length=PREVIEW_COLUMNS)
    duplicate_rows: Literal["reject", "keep-all"]
    duplicate_row_count: int = Field(ge=0, le=MAX_ROWS, strict=True)
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scope: Literal["whole-sheet-declared-columns"] = "whole-sheet-declared-columns"
    rows: Literal["present-rows-only"] = "present-rows-only"
    values: Literal["digest-and-counts-not-returned"] = "digest-and-counts-not-returned"
    formula_cached_values: Literal["never-trusted"] = "never-trusted"
    missing_tokens: Literal["literal-exact-match"] = "literal-exact-match"
    currencies: Literal["not-interpreted"] = "not-interpreted"
    percentages: Literal["not-interpreted"] = "not-interpreted"
    times_of_day: Literal["not-interpreted"] = "not-interpreted"
    duplicate_rows_handling: Literal["counted-not-dropped"] = "counted-not-dropped"
    dlp_scope: Literal["preview-text-only"] = "preview-text-only"
    storage: Literal["derived-not-persisted"] = "derived-not-persisted"

    @model_validator(mode="after")
    def report_is_complete(self) -> "XlsxConformanceResult":
        indices = [column.index for column in self.columns]
        if len(set(indices)) != len(indices):
            raise ValueError("xlsx_conformance_column_duplicate")
        if any(column.cells != self.data_rows for column in self.columns):
            raise ValueError("xlsx_conformance_counts_invalid")
        if self.cells_read != self.data_rows * len(self.columns):
            raise ValueError("xlsx_conformance_counts_invalid")
        if self.duplicate_row_count > self.data_rows:
            raise ValueError("xlsx_conformance_counts_invalid")
        return self


def conform_declared_columns(
    data: bytes,
    *,
    preview: XlsxPreviewData,
    interpretation: XlsxInterpretation,
    date_system: str,
    check: Callable[[], None],
) -> XlsxConformanceResult:
    """Read every present row below the header for the declared columns; return counts only."""
    if date_system not in ("1900", "1904"):
        raise ValueError("xlsx_date_system_invalid")
    labels = declared_header_labels(preview, interpretation.header_row)
    if labels is None or any(
        column.index >= len(labels) or column.header != labels[column.index]
        for column in interpretation.columns
    ):
        raise ValueError("xlsx_interpretation_binding_invalid")
    if not interpretation.columns:
        raise ValueError("xlsx_conformance_columns_required")
    position = preview.row_numbers.index(interpretation.header_row)
    expected_rows = preview.row_count - position - 1
    worksheet, shared = reader._sheet_part(data, preview.sheet)
    root = reader._xml(worksheet, "xlsx_conformance_sheet_invalid")
    if root.tag != f"{{{MAIN}}}worksheet":
        raise ValueError("xlsx_conformance_sheet_invalid")
    declared = interpretation.columns
    # XML columns are 1-based; declarations are 0-based over the materialised header.
    wanted = {column.index + 1 for column in declared}
    counters = [Counter() for _ in declared]
    reasons = [Counter() for _ in declared]
    column_digests = [hashlib.sha256() for _ in declared]
    snapshot = hashlib.sha256()
    seen: set[str] = set()
    duplicate_row_count = 0
    data_rows = 0
    characters = 0
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
        if data_rows % 256 == 1:
            check()
        data_rows += 1
        if data_rows * len(declared) > MAX_CONFORMANCE_CELLS:
            raise XlsxIntakeLimit("xlsx_conformance_cell_limit")
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
        characters += sum(len(cell.raw) for cell in readings)
        if characters > MAX_CONFORMANCE_CHARS:
            raise XlsxIntakeLimit("xlsx_conformance_char_limit")
        values = [[cell.status, cell.reason, cell.value] for cell in readings]
        row_key = _digest(values)
        duplicate_row_count += row_key in seen
        seen.add(row_key)
        snapshot.update(
            json.dumps([row_number, values], ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )
        for index, cell in enumerate(readings):
            counters[index][cell.status] += 1
            if cell.value is None:
                reasons[index][cell.reason] += 1
            column_digests[index].update(
                json.dumps(
                    [row_number, cell.status, cell.reason, cell.value],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
    if not header_found or data_rows != expected_rows:
        raise ValueError("xlsx_conformance_binding_invalid")
    columns = tuple(
        XlsxColumnConformance(
            index=column.index,
            header=column.header,
            type=column.type,
            cells=data_rows,
            values=counters[index]["value"],
            missing=counters[index]["missing"],
            unparsed=counters[index]["unparsed"],
            not_trusted=counters[index]["not_trusted"],
            reasons=tuple(
                XlsxReasonCount(reason=reason, count=count)
                for reason, count in sorted(reasons[index].items())
            ),
            digest="sha256:" + column_digests[index].hexdigest(),
        )
        for index, column in enumerate(declared)
    )
    return XlsxConformanceResult(
        interpretation_hash=interpretation.interpretation_hash,
        header_row=interpretation.header_row,
        date_system=date_system,
        data_rows=data_rows,
        cells_read=data_rows * len(declared),
        columns=columns,
        duplicate_rows=interpretation.duplicate_rows,
        duplicate_row_count=duplicate_row_count,
        snapshot_digest="sha256:" + snapshot.hexdigest(),
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
                raise ValueError("xlsx_conformance_sheet_invalid")
        previous_column = column
        if column in wanted:
            text, _, _, kind = reader._cell(cell, shared, True)
            cells[column] = (text, kind)
    return cells


def _digest(value: list) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class XlsxConformanceReader:
    def __init__(self, intake: XlsxIntakeService):
        self.intake = intake

    def report(
        self, file_id: UUID, actor: ActorContext, token: str, *, deadline: float | None = None
    ) -> XlsxConformanceResult:
        """Derive the whole-sheet report; no write, no audit, no retained result."""
        check = self.intake.guard(actor, token, deadline=deadline)
        with self.intake.store.db() as db:
            _, payload, raw = self.intake.store.owned(db, file_id, actor)
            bound = self.intake._bound_guard(payload, check)
            interpretation = payload.interpretation
            if interpretation is None:
                raise XlsxIntakeConflict("xlsx_interpretation_not_declared")
            if not interpretation.columns:
                raise XlsxIntakeConflict("xlsx_conformance_no_declared_columns")
            self.intake._parsed(payload, raw, bound)
            result = conform_declared_columns(
                raw,
                preview=payload.preview,
                interpretation=interpretation,
                date_system=payload.preflight.date_system,
                check=bound,
            )
            bound()
            return result


__all__ = [
    "MAX_CONFORMANCE_CELLS",
    "MAX_CONFORMANCE_CHARS",
    "XlsxColumnConformance",
    "XlsxConformanceReader",
    "XlsxConformanceResult",
    "XlsxReasonCount",
    "conform_declared_columns",
]
