"""Bounded XLSX text preview: inert values, separate formula/cache facts, never evaluated."""

import io
import zipfile
from collections import Counter
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_preflight import (
    DOC_REL,
    MAIN,
    MAX_COLUMNS,
    MAX_ROWS,
    PKG_REL,
    XlsxPreflightReport,
    preflight_xlsx,
)

PREVIEW_ROWS = 5
PREVIEW_COLUMNS = 64
MAX_CELL_CHARS = 32_768
MAX_PREVIEW_CHARS = 65_536


class XlsxPreviewError(ValueError):
    """Stable preview rejection; the code is part of the preview contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class XlsxFormulaCell(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    reference: str = Field(min_length=2, max_length=12)
    formula: str = Field(max_length=MAX_CELL_CHARS)
    cached_text: str | None = Field(default=None, max_length=MAX_CELL_CHARS)


class XlsxPreviewData(ContractModel):
    """Sampled grid only; formula text and cached values are never interpreted."""

    schema_version: Literal["1.0"] = "1.0"
    parser: Literal["gda-xlsx-text-preview-v1"] = "gda-xlsx-text-preview-v1"
    sheet: str = Field(min_length=1, max_length=31)
    header_row: int = Field(ge=1, le=MAX_ROWS)
    row_count: int = Field(ge=1, le=MAX_ROWS)
    column_count: int = Field(ge=0, le=MAX_COLUMNS)
    header: tuple[str, ...] = Field(max_length=PREVIEW_COLUMNS)
    rows: tuple[tuple[str, ...], ...] = Field(max_length=PREVIEW_ROWS)
    formulas: tuple[XlsxFormulaCell, ...]
    empty_headers: tuple[int, ...]
    duplicate_headers: tuple[str, ...]
    columns_truncated: bool
    rows_truncated: bool
    values: Literal["inert-text-no-conversion"] = "inert-text-no-conversion"
    formulas_separate: Literal["formula-text-with-cached-flag"] = "formula-text-with-cached-flag"
    evaluation: Literal["never"] = "never"
    dates: Literal["serial-text-not-converted"] = "serial-text-not-converted"

    @model_validator(mode="after")
    def preview_shape(self) -> "XlsxPreviewData":
        if any(len(row) != len(self.header) for row in self.rows):
            raise ValueError("xlsx preview rows must match the header width")
        if self.columns_truncated != (len(self.header) < self.column_count):
            raise ValueError("xlsx preview column truncation flag does not match")
        if self.rows_truncated != (len(self.rows) < self.row_count - 1):
            raise ValueError("xlsx preview row truncation flag does not match")
        if any(index >= len(self.header) for index in self.empty_headers):
            raise ValueError("xlsx preview empty header index is out of range")
        if tuple(sorted(set(self.empty_headers))) != self.empty_headers:
            raise ValueError("xlsx preview empty header indices must be sorted and unique")
        if tuple(sorted(set(self.duplicate_headers))) != self.duplicate_headers:
            raise ValueError("xlsx preview duplicate headers must be sorted and unique")
        return self


class XlsxPreviewResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    preflight: XlsxPreflightReport
    data: XlsxPreviewData

    @model_validator(mode="after")
    def sheet_is_preflighted(self) -> "XlsxPreviewResult":
        if self.data.sheet not in {sheet.name for sheet in self.preflight.sheets}:
            raise ValueError("xlsx preview sheet is not in the preflight report")
        return self


def preview_xlsx(data: bytes, *, sheet: str | None = None) -> XlsxPreviewResult:
    """Parse a bounded text grid after preflight; no value conversion or formula evaluation."""
    report = preflight_xlsx(data)
    names = [item.name for item in report.sheets]
    if sheet is None:
        sheet = names[0]
    if type(sheet) is not str or sheet not in names:
        raise XlsxPreviewError("xlsx_preview_sheet_not_found")
    worksheet, shared = _sheet_part(data, sheet)
    root = _xml(worksheet, "xlsx_preview_sheet_invalid")
    if root.tag != f"{{{MAIN}}}worksheet":
        raise XlsxPreviewError("xlsx_preview_sheet_invalid")
    if len(shared) != report.shared_strings:
        raise XlsxPreviewError("xlsx_preview_shared_string_invalid")
    data_row = _preview(root, shared, sheet)
    return XlsxPreviewResult(preflight=report, data=data_row)


def _sheet_part(data: bytes, sheet: str) -> tuple[bytes, tuple[str, ...]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            workbook = _xml(archive.read("xl/workbook.xml"), "xlsx_preview_sheet_invalid")
            relationships = _xml(
                archive.read("xl/_rels/workbook.xml.rels"), "xlsx_preview_sheet_invalid"
            )
            sheets = workbook.find(f"{{{MAIN}}}sheets")
            entries = [] if sheets is None else list(sheets)
            chosen = next((entry for entry in entries if entry.get("name") == sheet), None)
            if chosen is None:
                raise XlsxPreviewError("xlsx_preview_sheet_not_found")
            relation_id = chosen.get(f"{{{DOC_REL}}}id")
            target = None
            for relation in relationships.findall(f"{{{PKG_REL}}}Relationship"):
                if relation.get("Id") == relation_id and relation.get("Type", "").endswith(
                    "/worksheet"
                ):
                    target = relation.get("Target")
                    break
            if not target:
                raise XlsxPreviewError("xlsx_preview_sheet_invalid")
            part = target.removeprefix("/") if target.startswith("/") else _join_worksheet(target)
            worksheet = archive.read(part)
            shared = (
                _shared_strings(archive.read("xl/sharedStrings.xml"))
                if "xl/sharedStrings.xml" in archive.namelist()
                else ()
            )
    except XlsxPreviewError:
        raise
    except (KeyError, zipfile.BadZipFile, OSError, ElementTree.ParseError):
        raise XlsxPreviewError("xlsx_preview_package_invalid") from None
    return worksheet, shared


def _join_worksheet(target: str) -> str:
    parts = []
    for part in ("xl", target):
        for segment in part.split("/"):
            if segment in {"", "."}:
                continue
            if segment == "..":
                if not parts:
                    raise XlsxPreviewError("xlsx_preview_sheet_invalid")
                parts.pop()
            else:
                parts.append(segment)
    return "/".join(parts)


def _xml(data: bytes, code: str) -> ElementTree.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise XlsxPreviewError("xlsx_preview_xml_declaration_not_allowed")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise XlsxPreviewError(code) from None


def _shared_strings(data: bytes) -> tuple[str, ...]:
    root = _xml(data, "xlsx_preview_shared_string_invalid")
    if root.tag != f"{{{MAIN}}}sst":
        raise XlsxPreviewError("xlsx_preview_shared_string_invalid")
    strings = []
    for item in root.findall(f"{{{MAIN}}}si"):
        text = "".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
        strings.append(_bounded(text, "xlsx_preview_cell_too_large"))
    return tuple(strings)


def _bounded(text: str, code: str = "xlsx_preview_cell_too_large") -> str:
    if len(text) > MAX_CELL_CHARS:
        raise XlsxPreviewError(code)
    return text


def _cell_reference(reference: str | None) -> tuple[int, int] | None:
    if reference is None:
        return None
    letters = ""
    for character in reference:
        if not character.isalpha():
            break
        letters += character.upper()
    digits = reference[len(letters) :]
    if not letters or not letters.isascii() or not digits.isascii() or not digits.isdigit():
        raise XlsxPreviewError("xlsx_preview_sheet_invalid")
    column = 0
    for character in letters:
        column = column * 26 + (ord(character) - 64)
    row = int(digits)
    if row < 1 or row > MAX_ROWS or column > MAX_COLUMNS:
        raise XlsxPreviewError("xlsx_preview_sheet_invalid")
    return column, row


def _column_letters(column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _reference(column: int, row: int) -> str:
    return f"{_column_letters(column)}{row}"


def _preview(root: ElementTree.Element, shared: tuple[str, ...], sheet: str) -> XlsxPreviewData:
    rows = root.findall(f".//{{{MAIN}}}row")
    if not rows:
        raise XlsxPreviewError("xlsx_preview_empty_sheet")
    grid: list[dict[int, str]] = []
    formulas: list[XlsxFormulaCell] = []
    row_numbers: list[int] = []
    column_count = 0
    characters = 0
    previous_row = 0
    for row_index, row in enumerate(rows):
        row_number = _row_number(row.get("r"), previous_row)
        previous_row = row_number
        row_numbers.append(row_number)
        materialize = row_index <= PREVIEW_ROWS
        values: dict[int, str] = {}
        previous_column = 0
        for cell in row.findall(f"{{{MAIN}}}c"):
            parsed = _cell_reference(cell.get("r"))
            if parsed is None:
                column, cell_row = previous_column + 1, row_number
            else:
                column, cell_row = parsed
                if cell_row != row_number:
                    raise XlsxPreviewError("xlsx_preview_sheet_invalid")
            previous_column = column
            column_count = max(column_count, column)
            text, formula, cached = _cell(cell, shared, materialize)
            if materialize:
                values[column] = text
                characters += len(text) + len(formula or "") + len(cached or "")
                if characters > MAX_PREVIEW_CHARS:
                    raise XlsxPreviewError("xlsx_preview_too_large")
            if formula is not None and row_index <= PREVIEW_ROWS and column <= PREVIEW_COLUMNS:
                formulas.append(
                    XlsxFormulaCell(
                        reference=_reference(column, row_number),
                        formula=formula,
                        cached_text=cached,
                    )
                )
        grid.append(values)
    if column_count == 0:
        raise XlsxPreviewError("xlsx_preview_empty_sheet")
    preview_columns = min(column_count, PREVIEW_COLUMNS)
    header = tuple(grid[0].get(column, "") for column in range(1, preview_columns + 1))
    sampled = tuple(
        tuple(row.get(column, "") for column in range(1, preview_columns + 1))
        for row in grid[1 : PREVIEW_ROWS + 1]
    )
    counts = Counter(value for value in header if value)
    return XlsxPreviewData(
        sheet=sheet,
        header_row=row_numbers[0],
        row_count=len(rows),
        column_count=column_count,
        header=header,
        rows=sampled,
        formulas=tuple(formulas),
        empty_headers=tuple(index for index, value in enumerate(header) if not value),
        duplicate_headers=tuple(sorted(value for value, count in counts.items() if count > 1)),
        columns_truncated=preview_columns < column_count,
        rows_truncated=len(sampled) < len(rows) - 1,
    )


def _row_number(reference: str | None, previous: int) -> int:
    if reference is None:
        row = previous + 1
    elif not reference.isascii() or not reference.isdigit():
        raise XlsxPreviewError("xlsx_preview_sheet_invalid")
    else:
        row = int(reference)
    if row <= previous or row > MAX_ROWS:
        raise XlsxPreviewError("xlsx_preview_sheet_invalid")
    return row


def _cell(
    cell: ElementTree.Element, shared: tuple[str, ...], materialize: bool
) -> tuple[str, str | None, str | None]:
    kind = cell.get("t")
    value = cell.find(f"{{{MAIN}}}v")
    raw = value.text if value is not None else None
    formula_element = cell.find(f"{{{MAIN}}}f")
    formula = None if formula_element is None else _bounded(formula_element.text or "")
    if formula is not None:
        cached = _cached(raw, kind, shared, materialize)
        return (cached or "") if materialize else "", formula, cached
    if kind == "s":
        if raw is None:
            raise XlsxPreviewError("xlsx_preview_shared_string_invalid")
        text = _shared(raw, shared, materialize)
        return text if materialize else "", None, None
    if kind == "inlineStr":
        if not materialize:
            return "", None, None
        return (
            _bounded("".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))),
            None,
            None,
        )
    return _bounded(raw or "") if materialize else "", None, None


def _shared(raw: str, shared: tuple[str, ...], materialize: bool) -> str:
    if not raw.isascii() or not raw.isdigit():
        raise XlsxPreviewError("xlsx_preview_shared_string_invalid")
    index = int(raw)
    if index >= len(shared):
        raise XlsxPreviewError("xlsx_preview_shared_string_invalid")
    return shared[index] if materialize else ""


def _cached(
    raw: str | None, kind: str | None, shared: tuple[str, ...], materialize: bool
) -> str | None:
    if raw is None:
        return None
    if kind == "s":
        return _shared(raw, shared, materialize)
    return _bounded(raw) if materialize else None
