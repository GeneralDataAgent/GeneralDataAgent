"""Local XLSX staging boundaries: preview-only, scoped, and explicitly unconfirmed."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_preflight import MAX_BYTES, XlsxPreflightReport
from generaldataagent.datasets.xlsx_preview import XlsxPreviewData
from generaldataagent.memory.display_preferences import canonical_hash


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


class XlsxFilePreview(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file: XlsxFileRecord
    preflight: XlsxPreflightReport
    data: XlsxPreviewData

    @model_validator(mode="after")
    def preview_binding(self):
        if (
            self.preflight.content_sha256 != self.file.source_hash
            or self.data.sheet != self.file.config.sheet
            or self.preflight.inspector != self.file.config.preflight
            or self.data.parser != self.file.config.parser
        ):
            raise ValueError("xlsx_preview_binding_invalid")
        return self


class _XlsxPayload(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    actor: ActorContext
    file: XlsxFileRecord
    preview: XlsxPreviewData
    preflight: XlsxPreflightReport
