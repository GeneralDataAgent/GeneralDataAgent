"""Local XLSX boundaries: scoped preview, explicit confirmation and declared intent."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_preflight import MAX_BYTES, MAX_ROWS, XlsxPreflightReport
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS, XlsxPreviewData
from generaldataagent.memory.display_preferences import canonical_hash

MAX_DECLARED_HEADER_CHARS = 256
MAX_MISSING_TOKENS = 8
MAX_MISSING_TOKEN_CHARS = 32
DeclaredColumnType = Literal["text", "integer", "decimal", "boolean", "date"]


class XlsxIntakeError(ValueError):
    pass


class XlsxIntakeLimit(XlsxIntakeError):
    pass


class XlsxLimits(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=MAX_BYTES, strict=True)
    max_seconds: int = Field(default=10, ge=1, le=30, strict=True)


class XlsxTextConfig(ContractModel):
    """Pinned interpretation surface; later defaults cannot silently reinterpret a file."""

    schema_version: Literal["1.0"] = "1.0"
    parser: Literal["gda-xlsx-text-preview-v1"] = "gda-xlsx-text-preview-v1"
    preflight: Literal["gda-xlsx-preflight-v1"] = "gda-xlsx-preflight-v1"
    sheet: str = Field(min_length=1, max_length=31)
    header: Literal["first-row"] = "first-row"
    values: Literal["inert-text-no-conversion"] = "inert-text-no-conversion"
    formula_values: Literal["formula-text-with-cached-flag"] = "formula-text-with-cached-flag"
    evaluation: Literal["never"] = "never"
    dates: Literal["serial-text-not-converted"] = "serial-text-not-converted"


class XlsxFileRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parser_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    processing_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config: XlsxTextConfig
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preflight_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_count: int = Field(ge=1, le=MAX_BYTES, strict=True)
    status: Literal["staged", "confirmed"] = "staged"
    confirmation_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    confirmed_at: AwareDatetime | None = None
    dlp_scope: Literal["preview-text-only"] = "preview-text-only"
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def file_binding(self):
        if self.expires_at <= self.created_at or self.config_hash != canonical_hash(
            self.config.model_dump(mode="json")
        ):
            raise ValueError("xlsx_file_binding_invalid")
        if self.status == "staged":
            if self.confirmation_hash is not None or self.confirmed_at is not None:
                raise ValueError("xlsx_file_binding_invalid")
        elif (
            self.confirmation_hash != confirmation_digest(self)
            or self.confirmed_at is None
            or self.confirmed_at < self.created_at
            or self.confirmed_at >= self.expires_at
        ):
            raise ValueError("xlsx_file_binding_invalid")
        return self


def confirmation_digest(file: XlsxFileRecord) -> str:
    """Bind a confirmation to the exact staged facts, not to a later reinterpretation."""
    return canonical_hash(
        {
            "file_id": str(file.file_id),
            "source_hash": file.source_hash,
            "preview_hash": file.preview_hash,
            "parser_fingerprint": file.parser_fingerprint,
            "processing_fingerprint": file.processing_fingerprint,
            "config_hash": file.config_hash,
            "preflight_digest": file.preflight_digest,
            "sheet": file.config.sheet,
        }
    )


class ConfirmXlsxText(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    confirmed: bool = Field(strict=True)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("xlsx_explicit_text_confirmation_required")
        return self


class XlsxColumnDeclaration(ContractModel):
    """A declared intent for one previewed column; it never rewrites a cell value."""

    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    type: DeclaredColumnType
    empty_is_missing: bool = Field(default=True, strict=True)
    missing_tokens: tuple[str, ...] = Field(default=(), max_length=MAX_MISSING_TOKENS)

    @model_validator(mode="after")
    def missing_tokens_are_bounded(self) -> "XlsxColumnDeclaration":
        if any(not token or len(token) > MAX_MISSING_TOKEN_CHARS for token in self.missing_tokens):
            raise ValueError("xlsx_missing_token_invalid")
        if len(set(self.missing_tokens)) != len(self.missing_tokens):
            raise ValueError("xlsx_missing_token_duplicate")
        return self


def _column_indices_unique(columns: tuple[XlsxColumnDeclaration, ...]) -> None:
    indices = [column.index for column in columns]
    if len(set(indices)) != len(indices):
        raise ValueError("xlsx_column_declaration_duplicate")


class DeclareXlsxInterpretation(ContractModel):
    """Client declaration over a confirmed preview; the server still interprets nothing."""

    schema_version: Literal["1.0"] = "1.0"
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmation_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxColumnDeclaration, ...] = Field(default=(), max_length=PREVIEW_COLUMNS)
    duplicate_rows: Literal["reject", "keep-all"]

    @model_validator(mode="after")
    def columns_are_unique(self) -> "DeclareXlsxInterpretation":
        _column_indices_unique(self.columns)
        return self


class XlsxInterpretation(ContractModel):
    """Pinned declaration bound to one confirmed preview; declared-only, never applied."""

    schema_version: Literal["1.0"] = "1.0"
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxColumnDeclaration, ...] = Field(default=(), max_length=PREVIEW_COLUMNS)
    duplicate_rows: Literal["reject", "keep-all"]
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    values: Literal["declared-only-no-conversion"] = "declared-only-no-conversion"

    @model_validator(mode="after")
    def interpretation_binding(self) -> "XlsxInterpretation":
        _column_indices_unique(self.columns)
        if self.interpretation_hash != interpretation_digest(
            source_hash=self.source_hash,
            preview_hash=self.preview_hash,
            confirmation_hash=self.confirmation_hash,
            header_row=self.header_row,
            columns=self.columns,
            duplicate_rows=self.duplicate_rows,
        ):
            raise ValueError("xlsx_interpretation_binding_invalid")
        return self


def interpretation_digest(
    *,
    source_hash: str,
    preview_hash: str,
    confirmation_hash: str,
    header_row: int,
    columns: tuple[XlsxColumnDeclaration, ...],
    duplicate_rows: str,
) -> str:
    """Hash only the declaration; replaying the same intent stays idempotent."""
    return canonical_hash(
        {
            "source_hash": source_hash,
            "preview_hash": preview_hash,
            "confirmation_hash": confirmation_hash,
            "header_row": header_row,
            "columns": [column.model_dump(mode="json") for column in columns],
            "duplicate_rows": duplicate_rows,
        }
    )


def declared_header_labels(preview: XlsxPreviewData, header_row: int) -> tuple[str, ...] | None:
    """Labels of the declared header row, limited to the materialized preview window."""
    if header_row not in preview.row_numbers:
        return None
    position = preview.row_numbers.index(header_row)
    return preview.header if position == 0 else preview.rows[position - 1]


def interpretation_binds(
    file: XlsxFileRecord,
    preview: XlsxPreviewData,
    interpretation: XlsxInterpretation,
) -> bool:
    if file.status != "confirmed" or file.confirmation_hash is None:
        return False
    if (
        interpretation.source_hash != file.source_hash
        or interpretation.preview_hash != file.preview_hash
        or interpretation.confirmation_hash != file.confirmation_hash
        or interpretation.interpretation_hash
        != interpretation_digest(
            source_hash=interpretation.source_hash,
            preview_hash=interpretation.preview_hash,
            confirmation_hash=interpretation.confirmation_hash,
            header_row=interpretation.header_row,
            columns=interpretation.columns,
            duplicate_rows=interpretation.duplicate_rows,
        )
    ):
        return False
    labels = declared_header_labels(preview, interpretation.header_row)
    return labels is not None and all(
        0 <= column.index < len(labels) and column.header == labels[column.index]
        for column in interpretation.columns
    )


class XlsxFilePreview(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file: XlsxFileRecord
    preflight: XlsxPreflightReport
    data: XlsxPreviewData
    interpretation: XlsxInterpretation | None = None

    @model_validator(mode="after")
    def preview_binding(self):
        if (
            self.preflight.content_sha256 != self.file.source_hash
            or self.data.sheet != self.file.config.sheet
            or self.preflight.inspector != self.file.config.preflight
            or self.data.parser != self.file.config.parser
        ):
            raise ValueError("xlsx_preview_binding_invalid")
        if self.interpretation is not None and not interpretation_binds(
            self.file, self.data, self.interpretation
        ):
            raise ValueError("xlsx_interpretation_binding_invalid")
        return self


class _XlsxPayload(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    actor: ActorContext
    file: XlsxFileRecord
    preview: XlsxPreviewData
    preflight: XlsxPreflightReport
    interpretation: XlsxInterpretation | None = None

    @model_validator(mode="after")
    def interpretation_belongs_to_file(self) -> "_XlsxPayload":
        if self.interpretation is not None and not interpretation_binds(
            self.file, self.preview, self.interpretation
        ):
            raise ValueError("xlsx_interpretation_binding_invalid")
        return self
