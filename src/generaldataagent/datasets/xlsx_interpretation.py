"""Deterministic, bounded interpretation of declared preview columns; never persisted or guessed."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_intake_contracts import (
    MAX_DECLARED_HEADER_CHARS,
    DeclaredColumnType,
    XlsxColumnDeclaration,
    XlsxInterpretation,
    declared_header_labels,
)
from generaldataagent.datasets.xlsx_preflight import MAX_ROWS
from generaldataagent.datasets.xlsx_preview import (
    MAX_CELL_CHARS,
    PREVIEW_COLUMNS,
    PREVIEW_ROWS,
    CellKind,
    XlsxPreviewData,
)

MAX_NUMERIC_CHARS = 64
MAX_SERIAL_1900 = 2_958_465  # 9999-12-31 in the 1900 date system
MAX_SERIAL_1904 = 2_957_003  # 9999-12-31 in the 1904 date system

InterpretationStatus = Literal["value", "missing", "unparsed", "not_trusted"]
InterpretationReason = Literal[
    "empty_cell",
    "empty_not_missing",
    "missing_token",
    "error_cell",
    "formula_cached_value_not_trusted",
    "kind_not_text",
    "kind_not_numeric",
    "kind_not_boolean",
    "invalid_number",
    "non_integral_number",
    "invalid_boolean",
    "invalid_serial",
    "fractional_serial",
    "out_of_range",
    "value_too_large",
    "unknown_cell_kind",
]
MISSING_REASONS = frozenset({"empty_cell", "missing_token"})
NOT_TRUSTED_REASONS = frozenset({"formula_cached_value_not_trusted"})


class XlsxInterpretedCell(ContractModel):
    """One sampled cell read through a declaration; the raw text is never rewritten."""

    schema_version: Literal["1.0"] = "1.0"
    row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    raw: str = Field(max_length=MAX_CELL_CHARS)
    kind: CellKind
    status: InterpretationStatus
    value: str | None = Field(default=None, max_length=MAX_CELL_CHARS)
    reason: InterpretationReason | None = None

    @model_validator(mode="after")
    def result_is_consistent(self) -> "XlsxInterpretedCell":
        if self.status == "value":
            if self.value is None or self.reason is not None:
                raise ValueError("xlsx_interpreted_cell_invalid")
            return self
        if self.value is not None or self.reason is None:
            raise ValueError("xlsx_interpreted_cell_invalid")
        allowed = {
            "missing": MISSING_REASONS,
            "not_trusted": NOT_TRUSTED_REASONS,
        }.get(self.status)
        if allowed is not None and self.reason not in allowed:
            raise ValueError("xlsx_interpreted_cell_invalid")
        if allowed is None and self.reason in MISSING_REASONS | NOT_TRUSTED_REASONS:
            raise ValueError("xlsx_interpreted_cell_invalid")
        return self


class XlsxInterpretedColumn(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    type: DeclaredColumnType
    cells: tuple[XlsxInterpretedCell, ...] = Field(max_length=PREVIEW_ROWS)

    @model_validator(mode="after")
    def rows_are_strictly_increasing(self) -> "XlsxInterpretedColumn":
        rows = [cell.row for cell in self.cells]
        if any(rows[index + 1] <= rows[index] for index in range(len(rows) - 1)):
            raise ValueError("xlsx_interpreted_column_rows_invalid")
        return self


class XlsxInterpretationResult(ContractModel):
    """Derived per-cell reading of the declared columns over the materialized preview only."""

    schema_version: Literal["1.0"] = "1.0"
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    date_system: Literal["1900", "1904"]
    columns: tuple[XlsxInterpretedColumn, ...] = Field(max_length=64)
    scope: Literal["materialized-preview-window"] = "materialized-preview-window"
    conversion: Literal["declared-columns-only"] = "declared-columns-only"
    formula_cached_values: Literal["never-trusted"] = "never-trusted"
    missing_tokens: Literal["literal-exact-match"] = "literal-exact-match"
    currencies: Literal["not-interpreted"] = "not-interpreted"
    percentages: Literal["not-interpreted"] = "not-interpreted"
    times_of_day: Literal["not-interpreted"] = "not-interpreted"
    duplicate_rows: Literal["not-evaluated"] = "not-evaluated"
    storage: Literal["derived-not-persisted"] = "derived-not-persisted"

    @model_validator(mode="after")
    def column_indices_are_unique(self) -> "XlsxInterpretationResult":
        indices = [column.index for column in self.columns]
        if len(set(indices)) != len(indices):
            raise ValueError("xlsx_interpreted_column_duplicate")
        return self


def interpret_preview(
    preview: XlsxPreviewData,
    interpretation: XlsxInterpretation,
    *,
    date_system: str,
) -> XlsxInterpretationResult:
    """Interpret only declared columns of the sampled grid; no extraction beyond it."""
    if date_system not in ("1900", "1904"):
        raise ValueError("xlsx_date_system_invalid")
    labels = declared_header_labels(preview, interpretation.header_row)
    if labels is None or any(
        column.index >= len(labels) or column.header != labels[column.index]
        for column in interpretation.columns
    ):
        raise ValueError("xlsx_interpretation_binding_invalid")
    position = preview.row_numbers.index(interpretation.header_row)
    row_numbers = preview.row_numbers[position + 1 :]
    values = preview.rows[position:]
    kinds = preview.cell_kinds[position:]
    columns = []
    for declaration in interpretation.columns:
        cells = tuple(
            interpret_cell(
                row=row_number,
                raw=row[declaration.index],
                kind=column_kinds[declaration.index],
                declaration=declaration,
                date_system=date_system,
            )
            for row_number, row, column_kinds in zip(row_numbers, values, kinds, strict=True)
        )
        columns.append(
            XlsxInterpretedColumn(
                index=declaration.index,
                header=declaration.header,
                type=declaration.type,
                cells=cells,
            )
        )
    return XlsxInterpretationResult(
        interpretation_hash=interpretation.interpretation_hash,
        header_row=interpretation.header_row,
        date_system=date_system,
        columns=tuple(columns),
    )


def interpret_cell(
    *,
    row: int,
    raw: str,
    kind: CellKind,
    declaration: XlsxColumnDeclaration,
    date_system: str,
) -> XlsxInterpretedCell:
    def cell(status: InterpretationStatus, value=None, reason=None) -> XlsxInterpretedCell:
        return XlsxInterpretedCell(
            row=row, raw=raw, kind=kind, status=status, value=value, reason=reason
        )

    if kind == "formula":
        return cell("not_trusted", reason="formula_cached_value_not_trusted")
    if kind == "error":
        return cell("unparsed", reason="error_cell")
    if raw in declaration.missing_tokens or (not raw and declaration.empty_is_missing):
        reason = "missing_token" if raw else "empty_cell"
        return cell("missing", reason=reason)
    if not raw:
        return cell("unparsed", reason="empty_not_missing")
    if declaration.type == "text":
        if kind != "text":
            return cell("unparsed", reason="kind_not_text")
        return cell("value", value=raw)
    if declaration.type == "boolean":
        if kind != "boolean":
            return cell("unparsed", reason="kind_not_boolean")
        if raw not in ("1", "0"):
            return cell("unparsed", reason="invalid_boolean")
        return cell("value", value="true" if raw == "1" else "false")
    if declaration.type in ("integer", "decimal", "date"):
        if kind != "number":
            return cell("unparsed", reason="kind_not_numeric")
        if len(raw) > MAX_NUMERIC_CHARS:
            return cell("unparsed", reason="value_too_large")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return cell("unparsed", reason="invalid_number")
        if not number.is_finite():
            return cell("unparsed", reason="invalid_number")
        if declaration.type == "decimal":
            return cell("value", value=_canonical_decimal(number))
        if number != number.to_integral_value():
            reason = "fractional_serial" if declaration.type == "date" else "non_integral_number"
            return cell("unparsed", reason=reason)
        integral = int(number)
        if declaration.type == "integer":
            return cell("value", value=str(integral) if integral != 0 else "0")
        converted, value = _serial_date(integral, date_system)
        if converted != "value":
            return cell("unparsed", reason=converted)
        return cell("value", value=value)
    return cell("unparsed", reason="unknown_cell_kind")


def _canonical_decimal(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def _serial_date(serial: int, date_system: str) -> tuple[str, str | None]:
    """Map an Excel date serial exactly; serial 60 in 1900 mode never becomes a date."""
    if date_system == "1900":
        if serial < 1 or serial > MAX_SERIAL_1900:
            return "out_of_range", None
        if serial == 60:
            return "invalid_serial", None
        origin = date(1899, 12, 31)
        # Excel's 1900 system contains the fictitious 1900-02-29 at serial 60, so every
        # later serial is one day ahead of the true day count.
        days = serial if serial < 60 else serial - 1
    else:
        if serial < 0 or serial > MAX_SERIAL_1904:
            return "out_of_range", None
        origin = date(1904, 1, 1)
        days = serial
    return "value", (origin + timedelta(days=days)).isoformat()
