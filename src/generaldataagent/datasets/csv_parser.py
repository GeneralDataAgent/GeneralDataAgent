"""Bounded UTF-8 comma/quote parsing with explicit lossless text semantics."""

import csv
import hashlib
import io
import platform
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

_LOADED_SOURCE_HASH = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class CsvIntakeError(ValueError):
    pass


class CsvIntakeLimit(CsvIntakeError):
    pass


class CsvLimits(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    max_bytes: int = Field(default=1048576, ge=1, le=1048576, strict=True)
    max_rows: int = Field(default=1000, ge=1, le=1000, strict=True)
    max_columns: int = Field(default=32, ge=1, le=32, strict=True)
    max_cell_bytes: int = Field(default=4096, ge=1, le=4096, strict=True)
    max_seconds: int = Field(default=10, ge=1, le=10, strict=True)
    preview_rows: int = Field(default=5, ge=1, le=5, strict=True)


class CsvTextConfig(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    parser: Literal["gda-csv-text-v1"] = "gda-csv-text-v1"
    encoding: Literal["utf-8-with-optional-bom"] = "utf-8-with-optional-bom"
    delimiter: Literal[","] = ","
    quote: Literal['"'] = '"'
    header: Literal["first-row"] = "first-row"
    types: Literal["all-text"] = "all-text"
    empty_values: Literal["preserve-empty-string"] = "preserve-empty-string"
    formula_values: Literal["inert-text-no-evaluation"] = "inert-text-no-evaluation"


class CsvPreviewData(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    columns: tuple[str, ...] = Field(min_length=1, max_length=32)
    row_count: int = Field(ge=1, le=1000)
    sample_rows: tuple[tuple[str, ...], ...] = Field(max_length=5)
    empty_cells: tuple[int, ...]
    duplicate_rows: int = Field(ge=0)
    formula_like_cells: int = Field(ge=0)

    @model_validator(mode="after")
    def preview_shape(self):
        width = len(self.columns)
        if (
            len(self.empty_cells) != width
            or any(not 0 <= n <= self.row_count for n in self.empty_cells)
            or self.duplicate_rows >= self.row_count
            or self.formula_like_cells > self.row_count * width
            or len(self.sample_rows) > min(5, self.row_count)
            or any(len(row) != width for row in self.sample_rows)
        ):
            raise ValueError("csv_preview_shape_invalid")
        return self


@dataclass(frozen=True)
class ParsedCsv:
    preview: CsvPreviewData
    rows: tuple[tuple[str, ...], ...]


def parser_fingerprint(limits: CsvLimits) -> str:
    return canonical_hash(
        {
            "module_hash": _LOADED_SOURCE_HASH,
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "config": CsvTextConfig().model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
        }
    )


def _check_quoting(text: str, check, max_cell_bytes: int) -> None:
    # Reject ambiguous quotes the stdlib reader can accept in an unquoted field.
    # field/start -> quoted -> closed permits only comma/newline/end or a doubled quote.
    state, raw_field_bytes = "start", 0
    for index, char in enumerate(text):
        if index % 4096 == 0:
            check()
        if char == "\x00" or (unicodedata.category(char) == "Cc" and char not in "\r\n\t"):
            raise CsvIntakeError("csv_control_character_denied")
        if state != "quoted" and (char == "," or char in "\r\n"):
            raw_field_bytes = 0
        else:
            raw_field_bytes += len(char.encode())
            # A quoted field can at most double escaped quote bytes plus two wrappers.
            # Bound allocation before invoking the stdlib parser's process-global limit.
            if raw_field_bytes > 2 * max_cell_bytes + 2:
                raise CsvIntakeLimit("csv_cell_limit")
        if state == "quoted":
            if char == '"':
                state = "closed"
        elif state == "closed":
            if char == '"':
                state = "quoted"
            elif char == "," or char in "\r\n":
                state = "start"
            else:
                raise CsvIntakeError("csv_malformed_quotes")
        elif char == '"':
            if state != "start":
                raise CsvIntakeError("csv_malformed_quotes")
            state = "quoted"
        elif char == "," or char in "\r\n":
            state = "start"
        else:
            state = "field"
    if state == "quoted":
        raise CsvIntakeError("csv_malformed_quotes")


def parse_csv(content: bytes, limits: CsvLimits, check) -> ParsedCsv:
    if not isinstance(content, bytes) or not content or len(content) > limits.max_bytes:
        raise CsvIntakeLimit("csv_byte_limit_or_empty")
    if content.startswith((b"PK\x03\x04", b"\x1f\x8b", b"\xd0\xcf\x11\xe0")):
        raise CsvIntakeError("csv_compressed_or_workbook_denied")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CsvIntakeError("csv_utf8_required") from exc
    _check_quoting(text, check, limits.max_cell_bytes)
    rows = []
    try:
        for row in csv.reader(
            io.StringIO(text, newline=""), delimiter=",", quotechar='"', strict=True
        ):
            check()
            if not row:
                raise CsvIntakeError("csv_blank_record_denied")
            if len(row) > limits.max_columns or any(
                len(cell.encode()) > limits.max_cell_bytes for cell in row
            ):
                raise CsvIntakeLimit("csv_column_or_cell_limit")
            if len(rows) > limits.max_rows:
                raise CsvIntakeLimit("csv_row_limit")
            rows.append(tuple(row))
    except csv.Error as exc:
        raise CsvIntakeError("csv_malformed_record") from exc
    if len(rows) < 2:
        raise CsvIntakeError("csv_header_and_data_required")
    columns = rows[0]
    normalized = [unicodedata.normalize("NFKC", c).casefold() for c in columns]
    if len(set(normalized)) != len(columns) or any(
        not c
        or c != c.strip()
        or len(c) > 128
        or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in c)
        for c in columns
    ):
        raise CsvIntakeError("csv_header_invalid_or_duplicate")
    data = tuple(rows[1:])
    if any(len(row) != len(columns) for row in data):
        raise CsvIntakeError("csv_row_width_mismatch")
    empty = tuple(sum(row[i] == "" for row in data) for i in range(len(columns)))
    formula_like = sum(
        cell.lstrip().startswith(("=", "+", "-", "@")) for row in data for cell in row
    )
    preview = CsvPreviewData(
        columns=columns,
        row_count=len(data),
        sample_rows=data[: limits.preview_rows],
        empty_cells=empty,
        duplicate_rows=len(data) - len(set(data)),
        formula_like_cells=formula_like,
    )
    check()
    return ParsedCsv(preview, data)
