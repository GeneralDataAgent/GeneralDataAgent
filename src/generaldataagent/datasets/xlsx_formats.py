"""Whole-sheet number-format facts for declared XLSX columns; never applied or classified.

Format codes are inert text taken from the workbook's style table. The report counts which
formats each declared column uses; it never formats a value, never decides that a format is a
currency/percentage/time and never returns cell values.
"""

import io
import zipfile
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
from generaldataagent.datasets.xlsx_preflight import MAIN, MAX_ROWS
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS, XlsxPreviewData

MAX_FORMAT_CODE_CHARS = 256
MAX_FORMAT_STYLES = 4_096
MAX_FORMAT_CELLS = 250_000
BUILTIN_FORMAT_IDS = range(50)  # ECMA-376 builtin ids; some remain locale-dependent

FormatKind = Literal["builtin", "custom", "undefined"]


class XlsxFormatCode(ContractModel):
    """One distinct number format and how many declared cells use it; the code stays inert."""

    schema_version: Literal["1.0"] = "1.0"
    num_fmt_id: int = Field(ge=0, le=65_535, strict=True)
    kind: FormatKind
    code: str | None = Field(default=None, max_length=MAX_FORMAT_CODE_CHARS)
    cells: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def code_matches_kind(self) -> "XlsxFormatCode":
        if (self.kind == "custom") != (self.code is not None):
            raise ValueError("xlsx_format_code_invalid")
        return self


class XlsxColumnFormats(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    type: DeclaredColumnType
    cells: int = Field(ge=0, le=MAX_ROWS, strict=True)
    formats: tuple[XlsxFormatCode, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def formats_are_complete(self) -> "XlsxColumnFormats":
        if sum(item.cells for item in self.formats) != self.cells:
            raise ValueError("xlsx_formats_counts_invalid")
        keys = [(item.num_fmt_id, item.code or "") for item in self.formats]
        if keys != sorted(set(keys)):
            raise ValueError("xlsx_formats_order_invalid")
        return self


class XlsxFormatReport(ContractModel):
    """Derived display-format facts; nothing is formatted, classified, persisted or audited."""

    schema_version: Literal["1.0"] = "1.0"
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxColumnFormats, ...] = Field(max_length=PREVIEW_COLUMNS)
    scope: Literal["whole-sheet-declared-columns"] = "whole-sheet-declared-columns"
    rows: Literal["present-rows-only"] = "present-rows-only"
    display_format: Literal["inert-not-applied"] = "inert-not-applied"
    currencies: Literal["not-interpreted"] = "not-interpreted"
    percentages: Literal["not-interpreted"] = "not-interpreted"
    times_of_day: Literal["not-interpreted"] = "not-interpreted"
    classification: Literal["not-evaluated"] = "not-evaluated"
    values: Literal["unchanged"] = "unchanged"
    dlp: Literal["format-codes-scanned"] = "format-codes-scanned"
    storage: Literal["derived-not-persisted"] = "derived-not-persisted"

    @model_validator(mode="after")
    def columns_are_unique(self) -> "XlsxFormatReport":
        indices = [column.index for column in self.columns]
        if len(set(indices)) != len(indices):
            raise ValueError("xlsx_format_column_duplicate")
        if len({column.cells for column in self.columns}) > 1:
            raise ValueError("xlsx_formats_counts_invalid")
        return self


def read_declared_formats(
    data: bytes,
    *,
    preview: XlsxPreviewData,
    interpretation: XlsxInterpretation,
    check: Callable[[], None],
) -> XlsxFormatReport:
    """Count number formats per declared column over every present data row; values untouched."""
    labels = declared_header_labels(preview, interpretation.header_row)
    if labels is None or any(
        column.index >= len(labels) or column.header != labels[column.index]
        for column in interpretation.columns
    ):
        raise ValueError("xlsx_interpretation_binding_invalid")
    if not interpretation.columns:
        raise ValueError("xlsx_formats_columns_required")
    cell_formats, custom_codes = _style_table(data)
    worksheet, _ = reader._sheet_part(data, preview.sheet)
    root = reader._xml(worksheet, "xlsx_formats_sheet_invalid")
    if root.tag != f"{{{MAIN}}}worksheet":
        raise ValueError("xlsx_formats_sheet_invalid")
    declared = interpretation.columns
    positions = {column.index + 1: index for index, column in enumerate(declared)}
    counts = [Counter() for _ in declared]
    data_rows = 0
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
        if data_rows * len(declared) > MAX_FORMAT_CELLS:
            raise XlsxIntakeLimit("xlsx_formats_cell_limit")
        styled = _row_styles(element, row_number, positions, cell_formats)
        for column, num_fmt_id in styled.items():
            counts[positions[column]][num_fmt_id] += 1
        # A declared cell the worksheet omits carries no style record; it still counts as
        # the workbook default (General) so every declared cell is accounted for exactly once.
        for column in positions:
            if column not in styled:
                counts[positions[column]][0] += 1
    expected_rows = preview.row_count - preview.row_numbers.index(interpretation.header_row) - 1
    if not header_found or data_rows != expected_rows:
        raise ValueError("xlsx_formats_binding_invalid")
    columns = tuple(
        XlsxColumnFormats(
            index=column.index,
            header=column.header,
            type=column.type,
            cells=data_rows,
            formats=tuple(
                XlsxFormatCode(
                    num_fmt_id=num_fmt_id,
                    kind=_kind(num_fmt_id, custom_codes),
                    code=custom_codes.get(num_fmt_id),
                    cells=count,
                )
                for num_fmt_id, count in sorted(counts[index].items())
            ),
        )
        for index, column in enumerate(declared)
    )
    return XlsxFormatReport(
        interpretation_hash=interpretation.interpretation_hash,
        header_row=interpretation.header_row,
        columns=columns,
    )


def _kind(num_fmt_id: int, custom_codes: dict[int, str]) -> FormatKind:
    if num_fmt_id in custom_codes:
        return "custom"
    return "builtin" if num_fmt_id in BUILTIN_FORMAT_IDS else "undefined"


def _row_styles(
    element, row_number: int, positions: dict[int, int], cell_formats: tuple[int, ...]
) -> dict[int, int]:
    """Style indexes of the declared columns only; every other cell stays uninspected."""
    found: dict[int, int] = {}
    previous_column = 0
    for cell in element.findall(f"{{{MAIN}}}c"):
        parsed = reader._cell_reference(cell.get("r"))
        if parsed is None:
            column = previous_column + 1
        else:
            column, cell_row = parsed
            if cell_row != row_number:
                raise ValueError("xlsx_formats_sheet_invalid")
        previous_column = column
        if column not in positions:
            continue
        style = cell.get("s")
        if style is None:
            index = 0
        elif not style.isascii() or not style.isdigit():
            raise ValueError("xlsx_formats_style_invalid")
        else:
            index = int(style)
        if index >= len(cell_formats):
            raise ValueError("xlsx_formats_style_invalid")
        found[column] = cell_formats[index]
    return found


def _style_table(data: bytes) -> tuple[tuple[int, ...], dict[int, str]]:
    """Resolve `cellXfs` numFmtIds and the custom `numFmts` codes; bounded and fail closed."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "xl/styles.xml" not in archive.namelist():
                return (0,), {}
            raw = archive.read("xl/styles.xml")
    except (KeyError, zipfile.BadZipFile, OSError):
        raise ValueError("xlsx_formats_styles_invalid") from None
    root = reader._xml(raw, "xlsx_formats_styles_invalid")
    if root.tag != f"{{{MAIN}}}styleSheet":
        raise ValueError("xlsx_formats_styles_invalid")
    codes: dict[int, str] = {}
    for item in root.findall(f"{{{MAIN}}}numFmts/{{{MAIN}}}numFmt"):
        num_fmt_id = _num_fmt_id(item.get("numFmtId"))
        code = item.get("formatCode")
        if not code:
            raise ValueError("xlsx_formats_styles_invalid")
        if len(code) > MAX_FORMAT_CODE_CHARS:
            raise XlsxIntakeLimit("xlsx_formats_code_limit")
        if num_fmt_id in codes:
            raise ValueError("xlsx_formats_styles_invalid")
        codes[num_fmt_id] = code
        if len(codes) > MAX_FORMAT_STYLES:
            raise XlsxIntakeLimit("xlsx_formats_styles_limit")
    formats = []
    for xf in root.findall(f"{{{MAIN}}}cellXfs/{{{MAIN}}}xf"):
        formats.append(_num_fmt_id(xf.get("numFmtId") or "0"))
        if len(formats) > MAX_FORMAT_STYLES:
            raise XlsxIntakeLimit("xlsx_formats_styles_limit")
    return tuple(formats or [0]), codes


def _num_fmt_id(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdigit() or int(value) > 65_535:
        raise ValueError("xlsx_formats_styles_invalid")
    return int(value)


class XlsxFormatReader:
    def __init__(self, intake: XlsxIntakeService):
        self.intake = intake

    def report(
        self, file_id: UUID, actor: ActorContext, token: str, *, deadline: float | None = None
    ) -> XlsxFormatReport:
        """Derive display-format facts; format codes are DLP-scanned before returning."""
        check = self.intake.guard(actor, token, deadline=deadline)
        with self.intake.store.db() as db:
            _, payload, raw = self.intake.store.owned(db, file_id, actor)
            bound = self.intake._bound_guard(payload, check)
            interpretation = payload.interpretation
            if interpretation is None:
                raise XlsxIntakeConflict("xlsx_interpretation_not_declared")
            if not interpretation.columns:
                raise XlsxIntakeConflict("xlsx_formats_no_declared_columns")
            self.intake._parsed(payload, raw, bound)
            result = read_declared_formats(
                raw, preview=payload.preview, interpretation=interpretation, check=bound
            )
            codes = [item.code for column in result.columns for item in column.formats if item.code]
            if codes:
                try:
                    self.intake.scanner.check("\n".join(codes).encode())
                except PermissionError as exc:
                    raise PermissionError("xlsx_format_code_denied") from exc
            bound()
            return result
