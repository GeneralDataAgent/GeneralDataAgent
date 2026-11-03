"""Local CSV intake boundaries, separate from certified metric/task contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, ArtifactRef
from generaldataagent.contracts.data_engineering import DataProfile
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.csv_parser import CsvPreviewData, CsvTextConfig
from generaldataagent.memory.display_preferences import canonical_hash


class CsvFileRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parser_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    processing_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config: CsvTextConfig
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["staged", "profiled"]
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def file_binding(self):
        if self.expires_at <= self.created_at or self.config_hash != canonical_hash(
            self.config.model_dump(mode="json")
        ):
            raise ValueError("csv_file_binding_invalid")
        return self


class CsvFilePreview(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file: CsvFileRecord
    data: CsvPreviewData


class ConfirmCsvText(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    confirmed: bool = Field(strict=True)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("csv_explicit_text_confirmation_required")
        return self


class CsvProfileProvenance(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config: CsvTextConfig
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parser_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    semantics: Literal["all-text-empty-strings-preserved"] = "all-text-empty-strings-preserved"


class CsvProfileResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file: CsvFileRecord
    profile: DataProfile
    profile_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence: tuple[ArtifactRef, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def result_binding(self):
        if (
            self.file.status != "profiled"
            or self.profile_fingerprint != self.file.processing_fingerprint
            or any(ref.task_id != self.file.task_id for ref in self.evidence)
        ):
            raise ValueError("csv_profile_binding_invalid")
        return self


class _CsvPayload(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    actor: ActorContext
    file: CsvFileRecord
    preview: CsvPreviewData
    source_base64: str = Field(max_length=1398104)
